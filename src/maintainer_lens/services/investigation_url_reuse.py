"""Resolve repeated URL intent without mixing old checkpoints and new inputs."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.agents.investigation_analysis import ResponseLanguage
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.investigation_checkpoints import decode_frozen_graph_run
from maintainer_lens.db.investigation_results import decode_investigation_result
from maintainer_lens.db.investigation_reuse import InvestigationUrlStore
from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_checkpoint import InvestigationGraphRun
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.db.models.investigation_reuse import InvestigationTaskEvent
from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.messaging.schemas import (
    INVESTIGATION_REQUESTED_EVENT_TYPE,
    InvestigationRequestedMessage,
)
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_checkpoint import GraphCheckpointConfig
from maintainer_lens.schemas.investigation_contracts import InvestigationIdentity
from maintainer_lens.schemas.investigation_reuse import (
    InvestigationFreshness,
    ResolveInvestigationResponse,
)
from maintainer_lens.services.investigation_configuration import (
    make_worker_execution_config,
)
from maintainer_lens.services.investigation_freshness import (
    IssueObservation,
    bounded_observation,
    historical_freshness,
    observe_issue,
    render_observed_status,
)
from maintainer_lens.services.investigation_report import render_investigation_report
from maintainer_lens.services.investigation_submission import (
    add_investigation_submission,
)

CHECK_LEASE_SECONDS = 30
MAX_USER_RECOVERIES = 3


def stored_freshness(task) -> InvestigationFreshness:
    if task.freshness_payload is None:
        return InvestigationFreshness()
    return InvestigationFreshness.model_validate_json(
        json.dumps(task.freshness_payload), strict=True
    )


async def report_for_task(session, task, *, freshness_override=None):
    if task.status != "succeeded":
        return None
    record = await session.get(InvestigationResultRecord, task.id)
    if record is None:
        return None
    result = decode_investigation_result(
        record,
        task_id=task.id,
        repository=task.repository,
        issue_number=task.issue_number,
        source_index_id=task.prepared_source_index_id
        if task.auto_prepare_knowledge
        else task.source_index_id,
    )
    if result.response_language != (getattr(task, "response_language", None) or "zh"):
        raise ValueError("Stored report language differs from its task")
    freshness = (
        freshness_override
        if freshness_override is not None
        and freshness_override.observed_issue_state is not None
        else stored_freshness(task)
    )
    report_freshness = freshness_override or stored_freshness(task)
    snapshot = await session.get(InvestigationIssueSnapshot, task.id)
    state = freshness.observed_issue_state or (snapshot.state if snapshot else None)
    observed_at = (
        freshness.checked_at
        if freshness.observed_issue_state
        else (snapshot.fetched_at if snapshot else None)
    )
    return task.id, render_observed_status(
        render_investigation_report(result),
        result.response_language,
        state=state,
        checked_at=observed_at,
        freshness=report_freshness,
    )


async def _response(
    session,
    lookup,
    task,
    decision,
    *,
    freshness=None,
    previous_task_id=None,
    previous_report=None,
):
    selected_freshness = freshness or stored_freshness(task)
    report = previous_report or await report_for_task(session, task)
    if report is not None and report[0] == task.id:
        lookup.report_task_id = task.id
    if report is None and getattr(lookup, "report_task_id", None) is not None:
        previous = await session.get(InvestigationTask, lookup.report_task_id)
        if previous is not None:
            if (
                previous.repository.casefold(),
                previous.issue_number,
                previous.knowledge_budget,
                getattr(previous, "response_language", None) or "zh",
            ) != (
                task.repository.casefold(),
                task.issue_number,
                task.knowledge_budget,
                getattr(task, "response_language", None) or "zh",
            ):
                raise ValueError("Previous report has a different investigation intent")
            selected_freshness = historical_freshness(selected_freshness)
            report = await report_for_task(
                session, previous, freshness_override=selected_freshness
            )
    if report is not None and report[0] != task.id:
        selected_freshness = historical_freshness(selected_freshness)
    return ResolveInvestigationResponse(
        task_id=task.id,
        status=task.status,
        generation=task.execution_generation,
        decision=decision,
        previous_task_id=previous_task_id,
        report_task_id=report[0] if report else None,
        report_markdown=report[1] if report else None,
        freshness=selected_freshness,
        response_language=getattr(task, "response_language", None) or "zh",
    )


async def _create(
    session, lookup, repository, issue_number, budget, response_language="en"
):
    message = await add_investigation_submission(
        session,
        repository=repository.casefold(),
        issue_number=issue_number,
        budget=budget,
        auto_prepare_knowledge=True,
        response_language=response_language,
    )
    lookup.task_id, lookup.check_token, lookup.check_expires_at = (
        message.task_id,
        None,
        None,
    )
    session.add(
        InvestigationTaskEvent(
            task_id=message.task_id, generation=0, kind="queued", phase="queued"
        )
    )
    task = await session.get(InvestigationTask, message.task_id)
    task.freshness_payload = InvestigationFreshness().model_dump(mode="json")
    return task


def _execution_config():
    return make_worker_execution_config(get_settings(), source_enabled=True)


async def resolve_investigation_url(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository: str,
    issue_number: int,
    knowledge_budget_profile="standard",
    response_language: ResponseLanguage = "en",
    observer: Callable[[str, int], Awaitable[IssueObservation]] = observe_issue,
    configuration_factory: Callable[[], GraphCheckpointConfig] = _execution_config,
) -> ResolveInvestigationResponse:
    """One lookup lock, bounded HTTP outside SQL, then a token-fenced decision."""
    identity = InvestigationIdentity.model_validate(
        {"repository": repository, "issue_number": issue_number}, strict=True
    )
    repository, issue_number = identity.repository, identity.issue_number
    if response_language not in ("zh", "en"):
        raise ValueError("Unsupported response language")
    budget = make_knowledge_preparation_budget(knowledge_budget_profile)
    payload = budget.model_dump(mode="json")
    store = InvestigationUrlStore(session_factory)
    language_options = (
        {"response_language": response_language} if response_language != "zh" else {}
    )
    async with store.locked(repository, issue_number, payload, **language_options) as (
        session,
        lookup,
        task,
    ):
        if task is None:
            task = await _create(
                session, lookup, repository, issue_number, budget, response_language
            )
            return await _response(session, lookup, task, "created")
        if task.status == "waiting_review":
            return await _response(session, lookup, task, "awaiting_review")
        if task.status in ("pending", "running"):
            return await _response(session, lookup, task, "active")
        if (
            task.status == "succeeded"
            and await report_for_task(session, task) is not None
        ):
            lookup.report_task_id = task.id
        now = await session.scalar(select(func.clock_timestamp()))
        if lookup.check_token and lookup.check_expires_at > now:
            return await _response(
                session,
                lookup,
                task,
                "checking",
                freshness=InvestigationFreshness(status="checking"),
            )
        token = uuid4().hex
        lookup.check_token, lookup.check_expires_at = (
            token,
            now + timedelta(seconds=CHECK_LEASE_SECONDS),
        )
        task.freshness_payload = InvestigationFreshness(status="checking").model_dump(
            mode="json"
        )
        candidate_id, candidate_generation = task.id, task.execution_generation
        candidate_repository = task.repository

    try:
        observation, failure = await bounded_observation(
            observer, candidate_repository, issue_number
        )
    except asyncio.CancelledError:
        # Best-effort release; expiry remains the durable fallback if SQL is down.
        try:
            async with store.locked(
                repository, issue_number, payload, **language_options
            ) as (
                _,
                lookup,
                task,
            ):
                if lookup.check_token == token:
                    lookup.check_token, lookup.check_expires_at = None, None
                    if task is not None:
                        task.freshness_payload = InvestigationFreshness(
                            status="unknown",
                            reason_code="check_cancelled",
                            message="Latest metadata check was interrupted.",
                        ).model_dump(mode="json")
        except Exception:
            pass
        raise

    async with store.locked(repository, issue_number, payload, **language_options) as (
        session,
        lookup,
        task,
    ):
        now = await session.scalar(select(func.clock_timestamp()))
        if (
            task is None
            or task.id != candidate_id
            or task.execution_generation != candidate_generation
            or lookup.check_token != token
            or lookup.check_expires_at <= now
        ):
            if (
                lookup.check_token == token
                and task is not None
                and task.id == candidate_id
            ):
                lookup.check_token, lookup.check_expires_at = None, None
                task.freshness_payload = InvestigationFreshness(
                    status="unknown",
                    reason_code="check_expired",
                    message="Latest metadata check expired before its decision could commit.",
                ).model_dump(mode="json")
            if task is None:
                task = await _create(
                    session, lookup, repository, issue_number, budget, response_language
                )
            decision = "active" if task.status in ("pending", "running") else "blocked"
            if task.status == "waiting_review":
                decision = "awaiting_review"
            if task.status == "succeeded":
                decision = (
                    "reused"
                    if stored_freshness(task).status == "current"
                    else "unverified"
                )
            if lookup.check_token and lookup.check_expires_at > now:
                decision = "checking"
            return await _response(session, lookup, task, decision)
        lookup.check_token, lookup.check_expires_at = None, None
        if failure is not None:
            task.freshness_payload = failure.model_dump(mode="json")
            return await _response(
                session,
                lookup,
                task,
                "unverified" if task.status == "succeeded" else "blocked",
            )

        assert observation is not None
        snapshot = await session.get(InvestigationIssueSnapshot, task.id)
        record = await session.get(InvestigationGraphRun, task.id)
        if record is not None and snapshot is None:
            task.freshness_payload = InvestigationFreshness(
                status="unknown",
                reason_code="saved_snapshot_missing",
                message="The saved Issue snapshot is unavailable for comparison.",
            ).model_dump(mode="json")
            return await _response(
                session,
                lookup,
                task,
                "unverified" if task.status == "succeeded" else "blocked",
            )
        index_id = task.prepared_source_index_id
        index = (
            await session.get(RepositoryIndex, index_id)
            if index_id is not None
            else None
        )
        baseline_commit = index.commit_sha if index is not None else None
        freshness = InvestigationFreshness(
            status="current",
            checked_at=observation.checked_at,
            observed_issue_state=observation.issue.state,
            observed_commit_sha=observation.commit_sha,
            report_commit_sha=baseline_commit,
        )
        changed = False
        reason = None
        if snapshot is not None and (
            snapshot.title,
            snapshot.body,
            snapshot.state,
            snapshot.source_updated_at,
        ) != (
            observation.issue.title,
            observation.issue.body,
            observation.issue.state,
            observation.issue.updated_at,
        ):
            changed, reason = True, "issue_changed"
        if index_id is not None and (
            index is None
            or index.status != "ready"
            or baseline_commit != observation.commit_sha
        ):
            changed, reason = True, "source_changed"
        if record is not None:
            try:
                run = decode_frozen_graph_run(record)
                if (
                    run.request.repository,
                    run.request.issue_number,
                    run.request.source_index_id,
                ) != (
                    task.repository,
                    task.issue_number,
                    task.prepared_source_index_id,
                ):
                    raise ValueError("Saved execution changed task identity")
            except ValueError:
                task.freshness_payload = freshness.model_copy(
                    update={
                        "status": "unknown",
                        "reason_code": "invalid_saved_execution",
                        "message": "Saved execution could not be validated.",
                    }
                ).model_dump(mode="json")
                return await _response(session, lookup, task, "blocked")
            try:
                current_config = configuration_factory().model_copy(
                    update={"response_language": response_language}
                )
            except ValueError:
                task.freshness_payload = freshness.model_copy(
                    update={
                        "status": "unknown",
                        "reason_code": "configuration_unavailable",
                        "message": "Current execution configuration is unavailable.",
                    }
                ).model_dump(mode="json")
                return await _response(
                    session,
                    lookup,
                    task,
                    "unverified" if task.status == "succeeded" else "blocked",
                )
            if run.execution != current_config:
                changed, reason = True, "execution_changed"
            if index is None or (
                index.repository,
                index.embedding_provider,
                index.embedding_model,
                index.embedding_dimensions,
            ) != (
                task.repository,
                run.execution.embedding_provider,
                run.execution.embedding_model,
                run.execution.embedding_dimensions,
            ):
                changed, reason = True, "source_changed"
            if (run.request.issue_title, run.request.issue_body) != (
                observation.issue.title,
                observation.issue.body,
            ):
                changed, reason = True, "issue_changed"
        elif task.status == "succeeded":
            changed, reason = True, "execution_not_recorded"

        if observation.issue.number != issue_number:
            task.freshness_payload = InvestigationFreshness(
                status="unknown",
                reason_code="invalid_observation",
                message="GitHub returned unexpected Issue metadata.",
            ).model_dump(mode="json")
            return await _response(
                session,
                lookup,
                task,
                "unverified" if task.status == "succeeded" else "blocked",
            )
        if changed:
            freshness = freshness.model_copy(
                update={
                    "status": "stale",
                    "reason_code": reason,
                    "message": "Investigation inputs changed; a new investigation is queued.",
                }
            )
            task.freshness_payload = freshness.model_dump(mode="json")
            previous_id = task.id
            previous_report = await report_for_task(session, task)
            task = await _create(
                session, lookup, repository, issue_number, budget, response_language
            )
            return await _response(
                session,
                lookup,
                task,
                "created",
                freshness=freshness,
                previous_task_id=previous_id,
                previous_report=previous_report,
            )

        task.freshness_payload = freshness.model_dump(mode="json")
        if task.status == "succeeded":
            report = await report_for_task(session, task)
            if report is not None:
                return await _response(
                    session, lookup, task, "reused", previous_report=report
                )
            freshness = freshness.model_copy(
                update={
                    "status": "stale",
                    "reason_code": "report_missing",
                    "message": "No reusable report is stored; a new investigation is queued.",
                }
            )
            task.freshness_payload = freshness.model_dump(mode="json")
            previous_id = task.id
            task = await _create(
                session, lookup, repository, issue_number, budget, response_language
            )
            return await _response(
                session,
                lookup,
                task,
                "created",
                freshness=freshness,
                previous_task_id=previous_id,
            )

        if (
            not task.failure_retryable
            or task.user_recovery_count >= MAX_USER_RECOVERIES
        ):
            reason = (
                "recovery_limit"
                if task.user_recovery_count >= MAX_USER_RECOVERIES
                else "failure_not_recoverable"
            )
            task.freshness_payload = freshness.model_copy(
                update={
                    "reason_code": reason,
                    "message": "This failed task cannot be automatically resumed under its current policy.",
                }
            ).model_dump(mode="json")
            return await _response(session, lookup, task, "blocked")
        task.execution_generation += 1
        task.user_recovery_count += 1
        task.status, task.claim_token, task.lease_expires_at = "pending", None, None
        task.failure_retryable = False
        message = InvestigationRequestedMessage(
            task_id=task.id,
            repository=task.repository,
            issue_number=task.issue_number,
            auto_prepare_knowledge=True,
            generation=task.execution_generation,
            response_language=getattr(task, "response_language", None) or "zh",
        )
        session.add(
            InvestigationOutboxEvent(
                task_id=task.id,
                event_type=INVESTIGATION_REQUESTED_EVENT_TYPE,
                generation=message.generation,
                payload=message.model_dump(mode="json"),
            )
        )
        session.add(
            InvestigationTaskEvent(
                task_id=task.id,
                generation=message.generation,
                kind="queued",
                phase="queued",
            )
        )
        return await _response(session, lookup, task, "resumed")
