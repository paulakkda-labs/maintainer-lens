"""Coordinate investigation execution and lease maintenance."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maintainer_lens.db.investigation_progress import TaskEventRecorder

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    mark_investigation_failed,
    mark_investigation_succeeded,
    release_investigation_task_for_retry,
    renew_investigation_lease,
)

logger = logging.getLogger(__name__)


class LeaseLostError(RuntimeError):
    """Raised when a worker loses ownership of an investigation task."""


class InvestigationReviewRequired(Exception):
    """A processor supplied a validated review snapshot instead of a terminal result."""

    def __init__(self, context):
        super().__init__("Investigation requires a human budget decision")
        from maintainer_lens.schemas.investigation_review import ReviewContext

        self.context = ReviewContext.model_validate_json(
            context.model_dump_json(), strict=True
        )


class InvestigationClaimUnavailableError(RuntimeError):
    """Raised when an investigation task cannot be claimed."""


class RetryableInvestigationError(Exception):
    """An investigation failure that may be retried."""

    not_before: datetime | None = None


class PermanentInvestigationError(Exception):
    """An investigation failure that must not be retried."""


def _processing_error_leaves(error: Exception) -> list[Exception]:
    """Inspect nested processor failures without swallowing cancellation."""
    if isinstance(error, ExceptionGroup):
        return [
            leaf
            for child in error.exceptions
            for leaf in _processing_error_leaves(child)
        ]
    return [error]


def _classify_processing_error(
    error: Exception,
) -> RetryableInvestigationError | PermanentInvestigationError:
    """Map processor failures to the existing bounded delivery policy."""
    leaves = _processing_error_leaves(error)
    if any(isinstance(leaf, LeaseLostError) for leaf in leaves):
        raise error

    classified: list[RetryableInvestigationError | PermanentInvestigationError] = []
    for leaf in leaves:
        if isinstance(leaf, (RetryableInvestigationError, PermanentInvestigationError)):
            classified.append(leaf)
        elif isinstance(leaf, httpx.HTTPStatusError):
            status = leaf.response.status_code
            error_type = (
                RetryableInvestigationError
                if status >= 500 or status in (408, 409, 425, 429)
                else PermanentInvestigationError
            )
            classified.append(error_type(f"Upstream request failed: HTTP {status}"))
        elif isinstance(leaf, ValueError):
            classified.append(PermanentInvestigationError("Invalid investigation data"))
        else:
            # Unknown failures still have the existing finite retry budget.
            classified.append(
                RetryableInvestigationError("Investigation processor failed")
            )

    permanent = next(
        (item for item in classified if isinstance(item, PermanentInvestigationError)),
        None,
    )
    if permanent is not None:
        return permanent

    retryable = classified[0]
    deadlines = [item.not_before for item in classified if item.not_before is not None]
    if deadlines:
        retryable.not_before = max(deadlines)
    return retryable


async def maintain_investigation_lease(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    claim_token: str,
    stop_event: asyncio.Event,
    interval_seconds: float = 300.0,
) -> None:
    """Periodically renew an investigation lease until stopped."""
    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=interval_seconds,
            )
            return
        except TimeoutError:
            pass

        async with session_factory() as session:
            async with session.begin():
                renewed = await renew_investigation_lease(
                    session=session,
                    task_id=task_id,
                    claim_token=claim_token,
                )
        if not renewed:
            logger.warning(
                "Investigation lease lost: task_id=%s",
                task_id,
            )
            raise LeaseLostError("Investigation lease expired or ownership lost")

        logger.debug(
            "Investigation lease renewed: task_id=%s",
            task_id,
        )


async def run_investigation_with_lease(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    claim_token: str,
    processor: Callable[[], Awaitable[None]],
    renewal_interval_seconds: float = 300.0,
) -> None:
    """Run an investigation while maintaining its database lease."""
    stop_event = asyncio.Event()
    async with asyncio.TaskGroup() as tg:
        try:
            investigation_task = tg.create_task(processor())
            tg.create_task(
                maintain_investigation_lease(
                    session_factory=session_factory,
                    task_id=task_id,
                    claim_token=claim_token,
                    stop_event=stop_event,
                    interval_seconds=renewal_interval_seconds,
                )
            )
            await investigation_task
        finally:
            stop_event.set()


async def execute_investigation_task(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    processor: Callable[[str], Awaitable[None]],
    final_attempt: bool = False,
    expected_generation: int = 0,
    event_recorder: "TaskEventRecorder | None" = None,
) -> None:
    """Claim, execute, and complete an investigation task."""
    if type(expected_generation) is not int or expected_generation < 0:
        raise ValueError("Invalid execution generation")

    async with session_factory() as session:
        async with session.begin():
            claim_token = await claim_investigation_task(
                session=session,
                task_id=task_id,
                **(
                    {"expected_generation": expected_generation}
                    if expected_generation
                    else {}
                ),
            )
            if claim_token is not None and event_recorder is not None:
                await event_recorder(
                    session,
                    task_id=task_id,
                    claim_token=claim_token,
                    kind="started",
                    phase="starting",
                )
    if claim_token is None:
        raise InvestigationClaimUnavailableError(
            "Investigation task cannot be claimed."
        )

    processing_error: (
        RetryableInvestigationError | PermanentInvestigationError | None
    ) = None
    error_info = None
    review_context = None

    async def guarded_processor() -> None:
        nonlocal processing_error, error_info, review_context

        try:
            await processor(claim_token)
        except LeaseLostError:
            raise
        except InvestigationReviewRequired as exc:
            review_context = exc.context
        except Exception as exc:
            processing_error = _classify_processing_error(exc)
            if event_recorder is not None:
                from maintainer_lens.services.investigation_progress import (
                    describe_failure,
                )

                error_info = describe_failure(
                    exc,
                    retryable=isinstance(processing_error, RetryableInvestigationError),
                )
                if getattr(processing_error, "not_before", None) is not None:
                    error_info = error_info.model_copy(
                        update={"not_before": processing_error.not_before}
                    )

    await run_investigation_with_lease(
        session_factory=session_factory,
        task_id=task_id,
        claim_token=claim_token,
        processor=guarded_processor,
    )

    if review_context is not None:
        from maintainer_lens.db.investigation_review import suspend_task_for_review

        async with session_factory() as session, session.begin():
            await suspend_task_for_review(
                session,
                task_id=task_id,
                claim_token=claim_token,
                context=review_context,
            )
        return

    if processing_error is not None:
        async with session_factory() as session:
            async with session.begin():
                if event_recorder is not None:
                    await event_recorder(
                        session,
                        task_id=task_id,
                        claim_token=claim_token,
                        kind="failed"
                        if final_attempt
                        or isinstance(processing_error, PermanentInvestigationError)
                        else "retry_scheduled",
                        error=error_info,
                    )
                if final_attempt or isinstance(
                    processing_error,
                    PermanentInvestigationError,
                ):
                    changed = await mark_investigation_failed(
                        session=session,
                        task_id=task_id,
                        claim_token=claim_token,
                    )
                else:
                    changed = await release_investigation_task_for_retry(
                        session=session,
                        task_id=task_id,
                        claim_token=claim_token,
                    )

                if not changed:
                    raise LeaseLostError(
                        f"Cannot update investigation with an invalid lease: {task_id}"
                    )
        raise processing_error

    async with session_factory() as session:
        async with session.begin():
            if event_recorder is not None:
                await event_recorder(
                    session,
                    task_id=task_id,
                    claim_token=claim_token,
                    kind="completed",
                    phase="completed",
                )
            completed = await mark_investigation_succeeded(
                session=session,
                task_id=task_id,
                claim_token=claim_token,
            )
            if not completed:
                raise LeaseLostError(
                    f"Cannot complete investigation with an invalid lease: {task_id}"
                )
    logger.info("Investigation task succeeded: task_id=%s", task_id)
