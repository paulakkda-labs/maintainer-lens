"""Integration tests for worker lease maintenance."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import maintainer_lens.investigation_runner as runner_module
from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    create_investigation_task,
)
from maintainer_lens.db.models.investigation import InvestigationTask


@pytest.mark.anyio
@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration tests are disabled",
)
async def test_worker_lease_maintenance(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Verify renewal commits, graceful shutdown, and failure propagation."""
    # Create and claim a task.
    async with session_factory() as session:
        async with session.begin():
            await create_investigation_task(
                session=session,
                task_id=task_id,
                repository="fastapi/fastapi",
                issue_number=123,
            )

    async with session_factory() as session:
        async with session.begin():
            claim_token = await claim_investigation_task(
                session,
                task_id,
            )

    assert isinstance(claim_token, str)

    # Shorten the lease so renewal must extend it.
    short_expiry = datetime.now(UTC) + timedelta(minutes=1)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(lease_expires_at=short_expiry)
            )

    # Observe a real database renewal.
    renewal_called = asyncio.Event()
    original_renew = runner_module.renew_investigation_lease

    async def observed_renew(
        session: AsyncSession,
        task_id: str,
        claim_token: str,
    ) -> bool:
        result = await original_renew(
            session=session,
            task_id=task_id,
            claim_token=claim_token,
        )
        renewal_called.set()
        return result

    stop_event = asyncio.Event()

    # Replace the worker's renewal function temporarily.
    # The original database operation still executes.
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            runner_module,
            "renew_investigation_lease",
            observed_renew,
        )

        maintenance_task = asyncio.create_task(
            runner_module.maintain_investigation_lease(
                session_factory=session_factory,
                task_id=task_id,
                claim_token=claim_token,
                stop_event=stop_event,
                interval_seconds=0.01,
            )
        )

        try:
            # Wait until at least one renewal has occurred.
            await asyncio.wait_for(
                renewal_called.wait(),
                timeout=3,
            )
        finally:
            # Request shutdown and wait for the loop to exit.
            stop_event.set()
            await asyncio.wait_for(
                maintenance_task,
                timeout=3,
            )

        assert maintenance_task.done()
        assert not maintenance_task.cancelled()

        # An already-set stop event must prevent renewal.
        renewal_called.clear()

        already_stopped = asyncio.Event()
        already_stopped.set()

        await runner_module.maintain_investigation_lease(
            session_factory=session_factory,
            task_id=task_id,
            claim_token=claim_token,
            stop_event=already_stopped,
            interval_seconds=0.01,
        )

        assert not renewal_called.is_set()

    # A separate session must observe the committed lease.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.claim_token == claim_token
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > short_expiry + timedelta(minutes=10)

    # Losing ownership must raise LeaseLostError.
    with pytest.raises(runner_module.LeaseLostError):
        await asyncio.wait_for(
            runner_module.maintain_investigation_lease(
                session_factory=session_factory,
                task_id=task_id,
                claim_token="invalid-token",
                stop_event=asyncio.Event(),
                interval_seconds=0.01,
            ),
            timeout=3,
        )

    # Database errors must propagate to the caller.
    async def failing_renew(
        session: AsyncSession,
        task_id: str,
        claim_token: str,
    ) -> bool:
        raise RuntimeError("Simulated database failure")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            runner_module,
            "renew_investigation_lease",
            failing_renew,
        )

        with pytest.raises(
            RuntimeError,
            match="Simulated database failure",
        ):
            await asyncio.wait_for(
                runner_module.maintain_investigation_lease(
                    session_factory=session_factory,
                    task_id=task_id,
                    claim_token=claim_token,
                    stop_event=asyncio.Event(),
                    interval_seconds=0.01,
                ),
                timeout=3,
            )
