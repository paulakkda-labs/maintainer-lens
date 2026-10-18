"""Coordinate investigation execution and lease maintenance."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

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


class InvestigationClaimUnavailableError(RuntimeError):
    """Raised when an investigation task cannot be claimed."""


class RetryableInvestigationError(RuntimeError):
    """Raised when an investigation can be retried."""


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
    processor: Callable[[], Awaitable[None]],
    final_attempt: bool = False,
) -> None:
    """Claim, execute, and complete an investigation task."""

    async with session_factory() as session:
        async with session.begin():
            claim_token = await claim_investigation_task(
                session=session,
                task_id=task_id,
            )
    if claim_token is None:
        raise InvestigationClaimUnavailableError(
            "Investigation task cannot be claimed."
        )

    retry_error: RetryableInvestigationError | None = None

    async def guarded_processor() -> None:
        nonlocal retry_error

        try:
            await processor()
        except RetryableInvestigationError as exc:
            retry_error = exc

    await run_investigation_with_lease(
        session_factory=session_factory,
        task_id=task_id,
        claim_token=claim_token,
        processor=guarded_processor,
    )

    if retry_error is not None:
        async with session_factory() as session:
            async with session.begin():
                if final_attempt:
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
        raise retry_error

    async with session_factory() as session:
        async with session.begin():
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
