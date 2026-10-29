"""Review persistence under the original Task row and claim ownership."""

import json
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_checkpoints import decode_frozen_graph_run
from maintainer_lens.db.investigation_progress import append_task_event
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.models.investigation_checkpoint import (
    InvestigationCheckpoint,
    InvestigationGraphRun,
)
from maintainer_lens.db.models.investigation_review import InvestigationReview
from maintainer_lens.investigation_runner import LeaseLostError
from maintainer_lens.schemas.investigation_budget import KnowledgePreparationBudget
from maintainer_lens.schemas.investigation_progress import InvestigationErrorInfo
from maintainer_lens.schemas.investigation_review import ReviewContext, ReviewDecision


def decode_context(row: InvestigationReview) -> ReviewContext:
    return ReviewContext.model_validate_json(
        json.dumps(row.context_payload, allow_nan=False), strict=True
    )


def decode_decision(row: InvestigationReview) -> ReviewDecision | None:
    return (
        ReviewDecision.model_validate_json(
            json.dumps(row.decision_payload, allow_nan=False), strict=True
        )
        if row.decision_payload is not None
        else None
    )


async def lock_task(session: AsyncSession, task_id: str) -> InvestigationTask | None:
    return await session.scalar(
        select(InvestigationTask)
        .where(InvestigationTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def assert_owned(session: AsyncSession, task_id: str, claim_token: str) -> None:
    if (
        await session.scalar(
            select(InvestigationTask.id).where(
                InvestigationTask.id == task_id,
                InvestigationTask.status == "running",
                InvestigationTask.claim_token == claim_token,
                InvestigationTask.lease_expires_at > func.clock_timestamp(),
            )
        )
        is None
    ):
        raise LeaseLostError("Review operation requires the current active claim")


async def suspend_task_for_review(
    session: AsyncSession, *, task_id: str, claim_token: str, context: ReviewContext
) -> None:
    """Runner owns this transaction: review, event and lease release commit together."""
    context = ReviewContext.model_validate_json(context.model_dump_json(), strict=True)
    task = await lock_task(session, task_id)
    await assert_owned(session, task_id, claim_token)
    record = await session.get(InvestigationGraphRun, task_id)
    if context.run_fingerprint is not None:
        if record is None:
            raise ValueError("Review execution is missing")
        run = decode_frozen_graph_run(record)
        if (
            run.request.repository,
            run.request.issue_number,
            run.request.source_index_id,
        ) != (
            task.repository,
            task.issue_number,
            task.prepared_source_index_id
            if task.auto_prepare_knowledge
            else task.source_index_id,
        ):
            raise ValueError("Review execution belongs to another investigation scope")
        latest = await session.scalar(
            select(InvestigationCheckpoint.checkpoint_id)
            .where(
                InvestigationCheckpoint.task_id == task_id,
                InvestigationCheckpoint.checkpoint_ns == "",
            )
            .order_by(InvestigationCheckpoint.checkpoint_id.desc())
            .limit(1)
        )
        if (
            run.fingerprint != context.run_fingerprint
            or run.execution != context.base_execution
            or latest != context.checkpoint_id
        ):
            raise ValueError("Review does not match durable investigation progress")
    elif record is not None or context.reason == "analysis_generation_limit":
        raise ValueError("Review requires its frozen execution identity")
    session.add(
        InvestigationReview(
            id=uuid4(),
            task_id=task_id,
            generation=task.execution_generation,
            context_payload=context.model_dump(mode="json"),
        )
    )
    await append_task_event(
        session,
        task_id=task_id,
        claim_token=claim_token,
        kind="review_requested",
        phase="waiting_review",
        search_round=context.search_round,
        error=InvestigationErrorInfo(
            code=context.reason,
            message="Investigation paused for a human budget decision.",
            retryable=False,
        ),
    )
    paused = await session.scalar(
        update(InvestigationTask)
        .where(
            InvestigationTask.id == task_id,
            InvestigationTask.status == "running",
            InvestigationTask.claim_token == claim_token,
            InvestigationTask.lease_expires_at > func.clock_timestamp(),
        )
        .values(
            status="waiting_review",
            claim_token=None,
            lease_expires_at=None,
            failure_retryable=False,
        )
        .returning(InvestigationTask.id)
    )
    if paused is None:
        raise LeaseLostError("Review claim expired before suspension")
    await session.flush()


async def read_reviews(
    session: AsyncSession, task_id: str
) -> list[InvestigationReview]:
    return list(
        (
            await session.scalars(
                select(InvestigationReview)
                .where(InvestigationReview.task_id == task_id)
                .order_by(InvestigationReview.generation.desc())
                .limit(20)
            )
        ).all()
    )


async def load_approved_review(
    session_factory: async_sessionmaker[AsyncSession], *, task_id: str, claim_token: str
) -> tuple[KnowledgePreparationBudget, tuple[ReviewContext, ReviewDecision] | None]:
    """Return the latest immutable approval applicable to the current generation."""
    async with session_factory() as session, session.begin():
        task = await lock_task(session, task_id)
        await assert_owned(session, task_id, claim_token)
        row = await session.scalar(
            select(InvestigationReview)
            .where(
                InvestigationReview.task_id == task_id,
                InvestigationReview.target_generation <= task.execution_generation,
            )
            .order_by(InvestigationReview.target_generation.desc())
            .limit(1)
        )
        budget = KnowledgePreparationBudget.model_validate(
            task.knowledge_budget, strict=True
        )
        if row is None:
            return budget, None
        context, decision = decode_context(row), decode_decision(row)
        if decision is None or decision.action == "reject":
            raise ValueError("Invalid saved review approval")
        return budget, (context, decision)
