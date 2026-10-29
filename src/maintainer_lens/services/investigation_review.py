"""Build trusted review requests and apply bounded, idempotent human decisions."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.agents.investigation.checkpointing import (
    InvestigationCheckpointSaver,
)
from maintainer_lens.agents.investigation.routing import assess_next_step
from maintainer_lens.agents.investigation_analysis import (
    AnalysisGenerationIncompleteError,
)
from maintainer_lens.db.investigation_checkpoints import (
    InvestigationCheckpointStore,
    decode_frozen_graph_run,
)
from maintainer_lens.db.investigation_review import (
    decode_context,
    decode_decision,
    lock_task,
)
from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_checkpoint import (
    InvestigationCheckpoint,
    InvestigationGraphRun,
)
from maintainer_lens.db.models.investigation_reuse import InvestigationTaskEvent
from maintainer_lens.db.models.investigation_review import InvestigationReview
from maintainer_lens.messaging.schemas import (
    INVESTIGATION_REQUESTED_EVENT_TYPE,
    InvestigationRequestedMessage,
)
from maintainer_lens.schemas.investigation_budget import (
    KnowledgePreparationBudget,
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_checkpoint import GraphCheckpointConfig
from maintainer_lens.schemas.investigation_review import (
    MAX_BUDGET_RESUMES,
    ReviewContext,
    ReviewDecision,
    ReviewReason,
    ReviewResponse,
    allowed_preparation_profiles,
)
from maintainer_lens.services.investigation_checkpoint_execution import (
    _validate_resume_scope,
)
from maintainer_lens.services.investigation_execution import _validate_result
from maintainer_lens.services.investigation_preparation import (
    KnowledgePreparationDeadlineExceeded,
)
from maintainer_lens.services.repository_index_preparation import (
    KnowledgePreparationBudgetExceeded,
)


class ReviewConflictError(ValueError):
    """The addressed request is stale, incompatible or already decided differently."""


class ReviewNotFoundError(LookupError):
    """The task or review request does not exist in the requested scope."""


def review_trigger(error: Exception) -> tuple[ReviewReason, int | None] | None:
    leaves = []

    def visit(item):
        if isinstance(item, ExceptionGroup):
            for child in item.exceptions:
                visit(child)
        else:
            leaves.append(item)

    visit(error)
    # Mixed groups must retain their original semantics and structure.
    if len(leaves) != 1:
        return None
    leaf = leaves[0]
    if (
        isinstance(leaf, KnowledgePreparationBudgetExceeded)
        and leaf.limit_field is not None
    ):
        return leaf.limit_field, leaf.observed
    if isinstance(leaf, KnowledgePreparationDeadlineExceeded):
        return "preparation_timeout_seconds", None
    if (
        isinstance(leaf, AnalysisGenerationIncompleteError)
        and leaf.finish_reason == "length"
    ):
        return "analysis_generation_limit", None
    return None


def apply_approval(
    context: ReviewContext, decision: ReviewDecision
) -> tuple[KnowledgePreparationBudget, int]:
    budget, output = context.knowledge_budget, context.output_tokens
    if decision.action == "increase_budget":
        if context.reason == "analysis_generation_limit":
            if (
                decision.max_output_tokens is None
                or decision.max_output_tokens <= output
                or decision.knowledge_budget_profile is not None
            ):
                raise ValueError(
                    "Output approval must increase the current output budget"
                )
            output = decision.max_output_tokens
        else:
            if (
                decision.knowledge_budget_profile
                not in allowed_preparation_profiles(context)
                or decision.max_output_tokens is not None
            ):
                raise ValueError(
                    "Preparation approval must raise the affected limit without lowering others"
                )
            budget = make_knowledge_preparation_budget(
                decision.knowledge_budget_profile
            )
    elif decision.action == "accept_partial":
        if context.partial_result is None:
            raise ValueError("No complete validated analysis can be accepted")
    else:
        raise ValueError("A rejection cannot execute work")
    return budget, output


async def build_review_context(
    store: InvestigationCheckpointStore,
    *,
    trigger: tuple[ReviewReason, int | None],
    budget: KnowledgePreparationBudget,
    base_execution: GraphCheckpointConfig,
    output_tokens: int,
) -> ReviewContext:
    reason, observed = trigger
    run = await store.load_run()
    checkpoint_id, partial, novelty, round_number = None, None, False, None
    if run is not None:
        if run.execution != base_execution:
            raise ValueError("Review configuration differs from the frozen execution")
        saved = await InvestigationCheckpointSaver(store).aget_tuple(
            {"configurable": {"thread_id": store.task_id}}
        )
        if saved is not None:
            _validate_resume_scope(saved, run)
            checkpoint_id = saved.checkpoint["id"]
            state = dict(saved.checkpoint["channel_values"])
            round_number = state.get("search_round")
            # Only committed successful analysis is eligible; failed/pending node
            # writes are never promoted into an acceptable human report.
            if state.get("preliminary_analysis") is not None and state.get(
                "analysis_evidence"
            ):
                novelty = state.get("no_new_evidence", False)
                route, _ = assess_next_step(state)
                state.update(
                    next_step="draft_preliminary_report"
                    if route == "ready"
                    else "needs_more_evidence",
                    routing_reason="A complete previous analysis is available for human consideration; later investigation is unfinished.",
                )
                partial = _validate_result(
                    state, run.request, run.execution.max_search_rounds
                )
    if reason == "analysis_generation_limit" and (run is None or checkpoint_id is None):
        raise ValueError("Analysis review requires durable Graph progress")
    return ReviewContext(
        reason=reason,
        observed=observed,
        knowledge_budget=budget,
        base_execution=base_execution,
        output_tokens=output_tokens,
        run_fingerprint=run.fingerprint if run else None,
        checkpoint_id=checkpoint_id,
        partial_result=partial if reason == "analysis_generation_limit" else None,
        partial_no_new_evidence=novelty,
        search_round=round_number,
    )


def review_response(
    row: InvestigationReview,
    task: InvestigationTask,
    *,
    output_ceiling: int | None = None,
) -> ReviewResponse:
    context, decision = decode_context(row), decode_decision(row)
    if context.partial_result is not None:
        from maintainer_lens.services.investigation_report import (
            render_investigation_report,
        )

        render_investigation_report(context.partial_result)
        if (
            context.partial_result.repository,
            context.partial_result.issue_number,
            context.partial_result.source_index_id,
        ) != (
            task.repository,
            task.issue_number,
            task.prepared_source_index_id
            if task.auto_prepare_knowledge
            else task.source_index_id,
        ):
            raise ValueError("Review analysis belongs to a different investigation")
    pending = (
        decision is None
        and task.status == "waiting_review"
        and task.execution_generation == row.generation
    )
    can_increase = pending and task.budget_resume_count < MAX_BUDGET_RESUMES
    if (
        context.reason == "analysis_generation_limit"
        and output_ceiling is not None
        and output_ceiling <= context.output_tokens
    ):
        can_increase = False
    profiles = allowed_preparation_profiles(context)
    if context.reason != "analysis_generation_limit" and not profiles:
        can_increase = False
    return ReviewResponse(
        review_id=row.id,
        task_id=row.task_id,
        generation=row.generation,
        created_at=row.created_at,
        context=context,
        decision=decision,
        decided_at=row.decided_at,
        target_generation=row.target_generation,
        can_increase_budget=can_increase,
        allowed_knowledge_profiles=profiles if can_increase else [],
        can_accept_partial=pending and context.partial_result is not None,
        output_ceiling=output_ceiling,
        budget_resumes_used=task.budget_resume_count,
        user_recoveries_used=task.user_recovery_count,
    )


async def decide_review(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    task_id: str,
    review_id: UUID,
    decision: ReviewDecision,
    configuration_factory: Callable[[bool], GraphCheckpointConfig],
    output_ceiling: int | None = None,
) -> ReviewResponse:
    decision = ReviewDecision.model_validate_json(
        decision.model_dump_json(), strict=True
    )
    async with session_factory() as session, session.begin():
        task = await lock_task(session, task_id)
        row = await session.get(InvestigationReview, review_id)
        if task is None or row is None or row.task_id != task_id:
            raise ReviewNotFoundError("Review request not found")
        if row.decision_payload is not None:
            if decode_decision(row) != decision:
                raise ReviewConflictError(
                    "Review request already has a different decision"
                )
            return review_response(row, task, output_ceiling=output_ceiling)
        if (
            task.status != "waiting_review"
            or task.execution_generation != row.generation
        ):
            raise ReviewConflictError("Review request no longer owns the waiting task")
        context = decode_context(row)
        # A SELECT may autoflush dirty ORM objects. Read the clock before
        # changing the decision/time/target fields, which must be written together.
        decision_time = await session.scalar(select(func.clock_timestamp()))
        if decision.action != "reject":
            if (
                configuration_factory(context.base_execution.source_enabled)
                != context.base_execution
            ):
                raise ReviewConflictError(
                    "Deployment configuration changed; start a new investigation"
                )
            record = await session.get(InvestigationGraphRun, task_id)
            if context.run_fingerprint is not None:
                if (
                    record is None
                    or decode_frozen_graph_run(record).fingerprint
                    != context.run_fingerprint
                ):
                    raise ReviewConflictError("Frozen investigation changed")
                latest = await session.scalar(
                    select(InvestigationCheckpoint.checkpoint_id)
                    .where(
                        InvestigationCheckpoint.task_id == task_id,
                        InvestigationCheckpoint.checkpoint_ns == "",
                    )
                    .order_by(InvestigationCheckpoint.checkpoint_id.desc())
                    .limit(1)
                )
                if latest != context.checkpoint_id:
                    raise ReviewConflictError("Review checkpoint changed")
            elif record is not None:
                raise ReviewConflictError("Unexpected frozen execution appeared")
            apply_approval(context, decision)
            if decision.action == "increase_budget":
                if task.budget_resume_count >= MAX_BUDGET_RESUMES:
                    raise ReviewConflictError("Budget approval resume limit reached")
                if (
                    decision.max_output_tokens is not None
                    and output_ceiling is not None
                    and decision.max_output_tokens > output_ceiling
                ):
                    raise ValueError(
                        "Requested output budget exceeds the configured approval ceiling"
                    )
                task.budget_resume_count += 1
            task.execution_generation += 1
            task.status = "pending"
            row.target_generation = task.execution_generation
            message = InvestigationRequestedMessage(
                task_id=task.id,
                repository=task.repository,
                issue_number=task.issue_number,
                source_index_id=task.source_index_id,
                auto_prepare_knowledge=task.auto_prepare_knowledge,
                generation=task.execution_generation,
            )
            session.add(
                InvestigationOutboxEvent(
                    task_id=task.id,
                    generation=message.generation,
                    event_type=INVESTIGATION_REQUESTED_EVENT_TYPE,
                    payload=message.model_dump(mode="json"),
                )
            )
            kind, phase, error = "review_approved", "queued", None
        else:
            task.status, task.failure_retryable = "failed", False
            kind, phase = "review_rejected", "waiting_review"
            error = {
                "code": "review_rejected",
                "message": "A human rejected further investigation.",
                "retryable": False,
                "not_before": None,
            }
        task.claim_token, task.lease_expires_at = None, None
        row.decision_payload = decision.model_dump(mode="json")
        row.decided_at = decision_time
        session.add(
            InvestigationTaskEvent(
                task_id=task.id,
                generation=task.execution_generation,
                kind=kind,
                phase=phase,
                error_payload=error,
            )
        )
        await session.flush()
        return review_response(row, task, output_ceiling=output_ceiling)
