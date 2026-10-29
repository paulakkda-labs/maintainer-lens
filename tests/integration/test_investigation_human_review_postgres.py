"""Opt-in real SQL review/lease/outbox races and compiled Graph acceptance.

Apply Alembic head manually. Providers are simulated; no broker or paid calls.
Every test uses a unique Task and cascading cleanup, never shared user rows.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import delete, func, select, update

from maintainer_lens.agents.investigation.checkpointing import (
    InvestigationCheckpointSaver,
)
from maintainer_lens.agents.investigation_analysis import (
    AnalysisGenerationIncompleteError,
    InvestigationAnalysis,
)
from maintainer_lens.db.investigation_checkpoints import InvestigationCheckpointStore
from maintainer_lens.db.investigation_progress import (
    append_task_event,
    read_task_events,
)
from maintainer_lens.db.investigation_results import decode_investigation_result
from maintainer_lens.db.investigation_review import (
    load_approved_review,
    suspend_task_for_review,
)
from maintainer_lens.db.investigation_tasks import claim_investigation_task
from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.db.models.investigation_review import InvestigationReview
from maintainer_lens.investigation_runner import (
    InvestigationReviewRequired,
    LeaseLostError,
    execute_investigation_task,
)
from maintainer_lens.messaging.handler import process_investigation_request
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_checkpoint import GraphCheckpointConfig
from maintainer_lens.schemas.investigation_contracts import InvestigationInput
from maintainer_lens.schemas.investigation_review import ReviewContext, ReviewDecision
from maintainer_lens.services.investigation_checkpoint_execution import (
    execute_checkpointed_investigation,
)
from maintainer_lens.services.investigation_processor import process_graph_investigation
from maintainer_lens.services.investigation_review import (
    ReviewConflictError,
    build_review_context,
    decide_review,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def scope(session_factory):
    task_id = str(uuid4())
    repository = f"human-review-tests/repo-{uuid4().hex}"
    config = GraphCheckpointConfig(
        response_language="en",
        max_search_rounds=3,
        timeout_seconds=10.0,
        search_strategy="deterministic",
        source_enabled=False,
        history_top_k=5,
        source_top_k=5,
        embedding_provider="test",
        embedding_model="embed",
        embedding_dimensions=3,
        embedding_endpoint_fingerprint="1" * 64,
        llm_model="arbitrary-configured-model",
        llm_endpoint_fingerprint="2" * 64,
        llm_max_output_tokens=4096,
        thinking_enabled=False,
    )
    async with session_factory() as session, session.begin():
        session.add(
            InvestigationTask(
                id=task_id,
                repository=repository,
                issue_number=123,
                knowledge_budget=make_knowledge_preparation_budget("small").model_dump(
                    mode="json"
                ),
            )
        )
    data = SimpleNamespace(
        factory=session_factory, task_id=task_id, repository=repository, config=config
    )
    try:
        yield data
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == task_id)
            )


def _context(scope, *, budget=None, reason="source_files", observed=21):
    return ReviewContext(
        reason=reason,
        observed=observed,
        knowledge_budget=budget or make_knowledge_preparation_budget("small"),
        base_execution=scope.config,
        output_tokens=4096,
    )


async def _pause(scope, context=None):
    async def processor(token):
        current = context or _context(scope)
        if (
            current.reason == "analysis_generation_limit"
            and current.run_fingerprint is None
        ):
            store = InvestigationCheckpointStore(
                scope.factory, task_id=scope.task_id, claim_token=token
            )
            request = InvestigationInput(
                repository=scope.repository,
                issue_number=123,
                issue_title="Cleanup",
                issue_body=None,
                response_language="en",
            )
            await store.create_run(request, scope.config)
            frame = empty_checkpoint()
            frame["channel_values"] = {
                **request.model_dump(mode="json"),
                "search_round": 1,
                "no_new_evidence": False,
            }
            await InvestigationCheckpointSaver(store).aput(
                {"configurable": {"thread_id": scope.task_id}}, frame, {"step": 0}, {}
            )
            current = await build_review_context(
                store,
                trigger=("analysis_generation_limit", None),
                budget=current.knowledge_budget,
                base_execution=scope.config,
                output_tokens=current.output_tokens,
            )
        raise InvestigationReviewRequired(current)

    await execute_investigation_task(
        scope.factory,
        scope.task_id,
        processor,
        expected_generation=await _generation(scope),
        event_recorder=append_task_event,
    )
    async with scope.factory() as session:
        return await session.scalar(
            select(InvestigationReview)
            .where(InvestigationReview.task_id == scope.task_id)
            .order_by(InvestigationReview.generation.desc())
            .limit(1)
        )


async def _generation(scope):
    async with scope.factory() as session:
        return (
            await session.get(InvestigationTask, scope.task_id)
        ).execution_generation


async def _decide(scope, row, decision=None, **kwargs):
    return await decide_review(
        scope.factory,
        task_id=scope.task_id,
        review_id=row.id,
        decision=decision
        or ReviewDecision(
            decision_id=uuid4(),
            action="increase_budget",
            knowledge_budget_profile="standard",
        ),
        configuration_factory=lambda source: scope.config,
        **kwargs,
    )


async def test_postgres_pause_commits_review_and_phase_before_releasing_lease(scope):
    row = await _pause(scope)
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, scope.task_id)
        assert (
            task.status == "waiting_review"
            and task.claim_token is task.lease_expires_at is None
        )
        assert not task.failure_retryable and task.execution_generation == 0
        assert row.generation == 0 and row.decision_payload is None
        events = await read_task_events(session, task.id)
        assert events.events[-1].kind == "review_requested"
        assert events.events[-1].phase == "waiting_review"
    # Duplicate delivery is acknowledged by normal handler return, without work.
    processor = AsyncMock()
    await process_investigation_request(
        InvestigationRequestedMessage(
            task_id=scope.task_id, repository=scope.repository, issue_number=123
        ),
        scope.factory,
        processor,
    )
    processor.assert_not_awaited()


async def test_postgres_concurrent_identical_decisions_publish_one_generation_and_are_idempotent(
    scope,
):
    row = await _pause(scope)
    decision = ReviewDecision(
        decision_id=uuid4(),
        action="increase_budget",
        knowledge_budget_profile="standard",
    )
    results = await asyncio.gather(*[_decide(scope, row, decision) for _ in range(6)])
    assert all(
        result.target_generation == 1 and result.decision == decision
        for result in results
    )
    with pytest.raises(ReviewConflictError):
        await _decide(scope, row, ReviewDecision(decision_id=uuid4(), action="reject"))
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, scope.task_id)
        assert task.status == "pending" and task.budget_resume_count == 1
        assert task.user_recovery_count == 0 and task.execution_generation == 1
        assert task.knowledge_budget == make_knowledge_preparation_budget(
            "small"
        ).model_dump(mode="json")
        events = (
            await session.scalars(
                select(InvestigationOutboxEvent).where(
                    InvestigationOutboxEvent.task_id == task.id
                )
            )
        ).all()
        assert len(events) == 1 and events[0].generation == 1
        assert (
            InvestigationRequestedMessage.model_validate(events[0].payload).generation
            == 1
        )
    # Old generation cannot take the new claim.
    async with scope.factory() as session, session.begin():
        assert (
            await claim_investigation_task(
                session, scope.task_id, expected_generation=0
            )
            is None
        )
        assert await claim_investigation_task(
            session, scope.task_id, expected_generation=1
        )


async def test_postgres_pause_rolls_back_when_claim_expired(scope):
    async with scope.factory() as session, session.begin():
        token = await claim_investigation_task(session, scope.task_id)
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == scope.task_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    with pytest.raises(LeaseLostError):
        async with scope.factory() as session, session.begin():
            await suspend_task_for_review(
                session,
                task_id=scope.task_id,
                claim_token=token,
                context=_context(scope),
            )
    async with scope.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationReview)
                .where(InvestigationReview.task_id == scope.task_id)
            )
            == 0
        )
        assert (await session.get(InvestigationTask, scope.task_id)).status == "running"


async def test_postgres_late_approval_failure_rolls_back_decision_counter_event_and_outbox(
    scope, monkeypatch
):
    import maintainer_lens.services.investigation_review as service

    row = await _pause(scope)
    monkeypatch.setattr(
        service,
        "review_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("rollback after flush")
        ),
    )
    with pytest.raises(RuntimeError, match="after flush"):
        await _decide(scope, row)
    async with scope.factory() as session:
        saved = await session.get(InvestigationReview, row.id)
        task = await session.get(InvestigationTask, scope.task_id)
        assert (
            saved.decision_payload
            is saved.decided_at
            is saved.target_generation
            is None
        )
        assert (
            task.status == "waiting_review"
            and task.budget_resume_count == task.execution_generation == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationOutboxEvent)
                .where(InvestigationOutboxEvent.task_id == scope.task_id)
            )
            == 0
        )


async def test_postgres_three_budget_resumes_do_not_consume_user_recoveries_and_rejection_ends_wait(
    scope,
):
    budget = make_knowledge_preparation_budget("small")
    for profile, observed in (("standard", 21), ("large", 501), ("unlimited", 2001)):
        row = await _pause(scope, _context(scope, budget=budget, observed=observed))
        result = await _decide(
            scope,
            row,
            ReviewDecision(
                decision_id=uuid4(),
                action="increase_budget",
                knowledge_budget_profile=profile,
            ),
        )
        budget = make_knowledge_preparation_budget(profile)
        assert result.decision.knowledge_budget_profile == profile
    row = await _pause(
        scope,
        _context(
            scope, budget=budget, reason="analysis_generation_limit", observed=None
        ),
    )
    with pytest.raises(ReviewConflictError, match="limit"):
        await _decide(
            scope,
            row,
            ReviewDecision(
                decision_id=uuid4(), action="increase_budget", max_output_tokens=8192
            ),
        )
    await _decide(
        scope,
        row,
        ReviewDecision(
            decision_id=uuid4(), action="reject", note="No more work requested"
        ),
    )
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, scope.task_id)
        assert task.budget_resume_count == 3 and task.user_recovery_count == 0
        assert task.status == "failed" and not task.failure_retryable
        assert (await read_task_events(session, task.id)).events[
            -1
        ].error.code == "review_rejected"


async def test_postgres_approval_rejects_configuration_changes_or_wrong_task(scope):
    row = await _pause(scope)
    from maintainer_lens.services.investigation_review import ReviewNotFoundError

    with pytest.raises(ReviewNotFoundError):
        await decide_review(
            scope.factory,
            task_id="other-task",
            review_id=row.id,
            decision=ReviewDecision(decision_id=uuid4(), action="reject"),
            configuration_factory=lambda source: scope.config,
        )
    changed = scope.config.model_copy(update={"llm_model": "new-model"})
    with pytest.raises(ReviewConflictError, match="configuration"):
        await decide_review(
            scope.factory,
            task_id=scope.task_id,
            review_id=row.id,
            decision=ReviewDecision(
                decision_id=uuid4(),
                action="increase_budget",
                knowledge_budget_profile="standard",
            ),
            configuration_factory=lambda source: changed,
        )


async def test_postgres_output_approval_honors_optional_deployment_ceiling(scope):
    row = await _pause(
        scope, _context(scope, reason="analysis_generation_limit", observed=None)
    )
    decision = ReviewDecision(
        decision_id=uuid4(), action="increase_budget", max_output_tokens=8192
    )
    with pytest.raises(ValueError, match="ceiling"):
        await _decide(scope, row, decision, output_ceiling=4096)
    result = await _decide(scope, row, decision, output_ceiling=None)
    assert result.target_generation == 1


async def test_postgres_accept_complete_previous_analysis_stores_report_without_provider_calls(
    scope,
):
    request = InvestigationInput(
        repository=scope.repository,
        issue_number=123,
        issue_title="Cleanup",
        issue_body=None,
        response_language="en",
    )
    searcher = AsyncMock(
        side_effect=[
            (
                RepositoryHistorySemanticHit(
                    repository=scope.repository,
                    number=n,
                    source_type="item",
                    comment_id=None,
                    title="History",
                    content="Cleanup.",
                    source_url=f"https://github.com/{scope.repository}/issues/{n}",
                    source_updated_at=datetime.now(UTC),
                    distance=0.1,
                ),
            )
            for n in (124, 125)
        ]
    )
    complete = InvestigationAnalysis(
        summary="Complete first analysis.",
        findings=[
            {"statement": "Cleanup is discussed.", "evidence_ids": ["history:item:124"]}
        ],
        hypotheses=[],
        unanswered_questions=["What else can be checked?"],
    )
    analyzer = AsyncMock(
        side_effect=[complete, AnalysisGenerationIncompleteError("length")]
    )

    async def first(token):
        store = InvestigationCheckpointStore(
            scope.factory, task_id=scope.task_id, claim_token=token
        )
        run = await store.create_run(request, scope.config)
        try:
            await execute_checkpointed_investigation(
                run, store, history_searcher=searcher, analyzer=analyzer
            )
        except AnalysisGenerationIncompleteError:
            context = await build_review_context(
                store,
                trigger=("analysis_generation_limit", None),
                budget=make_knowledge_preparation_budget("small"),
                base_execution=scope.config,
                output_tokens=4096,
            )
            raise InvestigationReviewRequired(context) from None

    await execute_investigation_task(
        scope.factory, scope.task_id, first, event_recorder=append_task_event
    )
    async with scope.factory() as session:
        row = await session.scalar(
            select(InvestigationReview).where(
                InvestigationReview.task_id == scope.task_id
            )
        )
        assert (
            row.context_payload["partial_result"]["preliminary_analysis"]["summary"]
            == complete.summary
        )
    await _decide(
        scope, row, ReviewDecision(decision_id=uuid4(), action="accept_partial")
    )
    no_io = AsyncMock(
        side_effect=AssertionError("Accepted analysis must not call providers")
    )

    async def second(token):
        _, approval = await load_approved_review(
            scope.factory, task_id=scope.task_id, claim_token=token
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    AssertionError("No GitHub refresh")
                )
            )
        ) as client:
            await process_graph_investigation(
                http_client=client,
                session_factory=scope.factory,
                task_id=scope.task_id,
                claim_token=token,
                repository=scope.repository,
                issue_number=123,
                response_language="en",
                history_searcher=no_io,
                analyzer=no_io,
                timeout_seconds=10.0,
                max_search_rounds=3,
                checkpoint_store=InvestigationCheckpointStore(
                    scope.factory, task_id=scope.task_id, claim_token=token
                ),
                checkpoint_config=scope.config,
                review_approval=approval,
            )

    await execute_investigation_task(
        scope.factory,
        scope.task_id,
        second,
        expected_generation=1,
        event_recorder=append_task_event,
    )
    no_io.assert_not_awaited()
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, scope.task_id)
        record = await session.get(InvestigationResultRecord, scope.task_id)
        result = decode_investigation_result(
            record, task_id=scope.task_id, repository=scope.repository, issue_number=123
        )
        assert (
            task.status == "succeeded"
            and task.budget_resume_count == task.user_recovery_count == 0
        )
        assert result.search_round == 2 and result.next_step == "needs_more_evidence"
        assert result.preliminary_analysis.summary == complete.summary
        assert "Human accepted" in result.routing_reason


async def test_postgres_stale_undecided_review_cannot_resume_a_new_generation(scope):
    row = await _pause(scope)
    async with scope.factory() as session, session.begin():
        task = await session.get(InvestigationTask, scope.task_id)
        task.execution_generation = 1
    with pytest.raises(ReviewConflictError, match="no longer"):
        await _decide(scope, row)
    async with scope.factory() as session:
        assert (await session.get(InvestigationReview, row.id)).decision_payload is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationOutboxEvent)
                .where(InvestigationOutboxEvent.task_id == scope.task_id)
            )
            == 0
        )


async def test_postgres_pause_failure_after_event_flush_rolls_back_request_and_phase(
    scope, monkeypatch
):
    import maintainer_lens.db.investigation_review as persistence

    original_append = persistence.append_task_event
    async with scope.factory() as session, session.begin():
        token = await claim_investigation_task(session, scope.task_id)

    async def expire_after_event(session, **kwargs):
        await original_append(session, **kwargs)
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == scope.task_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )

    monkeypatch.setattr(persistence, "append_task_event", expire_after_event)
    with pytest.raises(LeaseLostError, match="before suspension"):
        async with scope.factory() as session, session.begin():
            await suspend_task_for_review(
                session,
                task_id=scope.task_id,
                claim_token=token,
                context=_context(scope),
            )
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, scope.task_id)
        assert task.status == "running" and task.claim_token == token
        assert (await read_task_events(session, task.id)).events == []
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationReview)
                .where(InvestigationReview.task_id == scope.task_id)
            )
            == 0
        )


@pytest.mark.parametrize("action", ["increase_budget", "reject"])
async def test_postgres_decision_payload_and_time_survive_default_autoflush_together(
    scope, action
):
    row = await _pause(scope)
    decision = ReviewDecision(
        decision_id=uuid4(),
        action=action,
        **(
            {"knowledge_budget_profile": "standard"}
            if action == "increase_budget"
            else {}
        ),
    )
    async with scope.factory() as session:
        assert session.autoflush is True
    response = await _decide(scope, row, decision)
    async with scope.factory() as session:
        stored = await session.get(InvestigationReview, row.id)
        assert stored.decision_payload == decision.model_dump(mode="json")
        assert (
            stored.decided_at is not None and stored.decided_at == response.decided_at
        )
        assert stored.decided_at.tzinfo is not None
        assert stored.target_generation == (1 if action == "increase_budget" else None)
        task = await session.get(InvestigationTask, scope.task_id)
        assert task.status == ("pending" if action == "increase_budget" else "failed")
