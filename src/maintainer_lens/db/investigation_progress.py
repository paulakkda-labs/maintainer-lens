"""Short, claim-fenced progress writes and bounded chronological reads."""

import json
from collections.abc import Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.models.investigation_reuse import InvestigationTaskEvent
from maintainer_lens.investigation_runner import LeaseLostError
from maintainer_lens.schemas.investigation_progress import (
    InvestigationErrorInfo,
    InvestigationEventKind,
    InvestigationEventPage,
    InvestigationPhase,
    InvestigationProgressEvent,
)

type TaskEventRecorder = Callable[..., Awaitable[None]]


def decode_event(row: InvestigationTaskEvent) -> InvestigationProgressEvent:
    return InvestigationProgressEvent.model_validate_json(
        json.dumps(
            {
                "event_id": row.id,
                "generation": row.generation,
                "kind": row.kind,
                "phase": row.phase,
                "search_round": row.search_round,
                "error": row.error_payload,
                "created_at": row.created_at.isoformat(),
            }
        ),
        strict=True,
    )


async def append_task_event(
    session: AsyncSession,
    *,
    task_id: str,
    claim_token: str,
    kind: InvestigationEventKind,
    phase: InvestigationPhase | None = None,
    error: InvestigationErrorInfo | None = None,
    search_round: int | None = None,
) -> None:
    """Caller commits; no provider request is executed while this row is locked."""
    task = await session.scalar(
        select(InvestigationTask)
        .where(InvestigationTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    async def owned():
        return await session.scalar(
            select(InvestigationTask.id).where(
                InvestigationTask.id == task_id,
                InvestigationTask.status == "running",
                InvestigationTask.claim_token == claim_token,
                InvestigationTask.lease_expires_at > func.clock_timestamp(),
            )
        )

    if task is None or await owned() is None:
        raise LeaseLostError("Progress requires the active claim")
    if phase is None:
        latest = await session.scalar(
            select(InvestigationTaskEvent)
            .where(
                InvestigationTaskEvent.task_id == task_id,
                InvestigationTaskEvent.generation == task.execution_generation,
            )
            .order_by(InvestigationTaskEvent.id.desc())
            .limit(1)
        )
        phase = latest.phase if latest else "starting"
        if search_round is None and latest is not None:
            search_round = latest.search_round
    if error is not None:
        task.failure_retryable = error.retryable
    event = InvestigationProgressEvent(
        event_id=0,
        generation=task.execution_generation,
        kind=kind,
        phase=phase,
        search_round=search_round,
        error=error,
        created_at=await session.scalar(select(func.clock_timestamp())),
    )
    session.add(
        InvestigationTaskEvent(
            task_id=task_id,
            generation=event.generation,
            kind=event.kind,
            phase=event.phase,
            search_round=event.search_round,
            error_payload=error.model_dump(mode="json") if error else None,
        )
    )
    await session.flush()
    if await owned() is None:
        raise LeaseLostError("Progress claim expired before commit")


async def read_task_events(
    session: AsyncSession, task_id: str, *, after: int = 0, limit: int = 50
) -> InvestigationEventPage:
    if (
        type(after) is not int
        or after < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("Invalid event page")
    rows = (
        await session.scalars(
            select(InvestigationTaskEvent)
            .where(
                InvestigationTaskEvent.task_id == task_id,
                InvestigationTaskEvent.id > after,
            )
            .order_by(InvestigationTaskEvent.id)
            .limit(limit + 1)
        )
    ).all()
    selected = rows[:limit]
    return InvestigationEventPage(
        events=[decode_event(row) for row in selected],
        next_after=selected[-1].id if len(rows) > limit else None,
    )


async def read_latest_progress(
    session: AsyncSession, task_id: str, *, generation: int, task_status: str
):
    statement = select(InvestigationTaskEvent).where(
        InvestigationTaskEvent.task_id == task_id,
        InvestigationTaskEvent.generation <= generation,
    )
    kinds = {
        "pending": ("queued", "retry_scheduled", "review_approved"),
        "running": ("started", "progress"),
        "succeeded": ("completed",),
        "failed": ("failed", "review_rejected"),
        "waiting_review": ("review_requested",),
    }
    latest = await session.scalar(
        statement.where(
            InvestigationTaskEvent.generation == generation,
            InvestigationTaskEvent.kind.in_(kinds[task_status]),
        )
        .order_by(InvestigationTaskEvent.id.desc())
        .limit(1)
    )
    last_error = await session.scalar(
        statement.where(InvestigationTaskEvent.error_payload.is_not(None))
        .order_by(InvestigationTaskEvent.id.desc())
        .limit(1)
    )
    return decode_event(latest) if latest else None, decode_event(
        last_error
    ).error if last_error else None
