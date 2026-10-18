"""Integration tests for investigation task execution."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import maintainer_lens.investigation_runner as runner_module
from maintainer_lens.db.investigation_tasks import claim_investigation_task
from maintainer_lens.db.models.investigation import InvestigationTask

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


async def test_successful_investigation_is_persisted(
    investigation_task: tuple[async_sessionmaker[AsyncSession], str, AsyncEngine],
) -> None:
    """Persist the final status after a successful investigation."""
    session_factory, task_id, _ = investigation_task

    processor_called = False

    async def processor() -> None:
        nonlocal processor_called
        processor_called = True

        # The claim must already be committed before processing.
        async with session_factory() as session:
            task = await session.get(InvestigationTask, task_id)

            assert task is not None
            assert task.status == "running"
            assert task.claim_token is not None
            assert task.lease_expires_at is not None

    await runner_module.execute_investigation_task(
        session_factory=session_factory,
        task_id=task_id,
        processor=processor,
    )

    assert processor_called

    # Verify the final status using a separate database session.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "succeeded"
        assert task.claim_token is None
        assert task.lease_expires_at is None


async def test_unavailable_claim_does_not_start_processor(
    investigation_task: tuple[async_sessionmaker[AsyncSession], str, AsyncEngine],
) -> None:
    """Do not execute an investigation when its task is already claimed."""
    session_factory, task_id, _ = investigation_task

    # Another worker claims the task first.
    async with session_factory() as session:
        async with session.begin():
            existing_token = await claim_investigation_task(
                session=session,
                task_id=task_id,
            )

    assert isinstance(existing_token, str)

    processor_called = False

    async def processor() -> None:
        nonlocal processor_called
        processor_called = True

    with pytest.raises(runner_module.InvestigationClaimUnavailableError):
        await runner_module.execute_investigation_task(
            session_factory=session_factory,
            task_id=task_id,
            processor=processor,
        )

    assert not processor_called

    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.claim_token == existing_token


async def test_processor_failure_does_not_mark_success(
    investigation_task: tuple[async_sessionmaker[AsyncSession], str, AsyncEngine],
) -> None:
    """Leave the task unfinished when investigation processing fails."""
    session_factory, task_id, _ = investigation_task

    async def processor() -> None:
        raise ValueError("Simulated investigation failure")

    with pytest.raises(ExceptionGroup) as exc_info:
        await runner_module.execute_investigation_task(
            session_factory=session_factory,
            task_id=task_id,
            processor=processor,
        )

    assert any(isinstance(exc, ValueError) for exc in exc_info.value.exceptions)

    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.claim_token is not None
        assert task.lease_expires_at is not None


async def test_expired_lease_prevents_final_completion(
    investigation_task: tuple[async_sessionmaker[AsyncSession], str, AsyncEngine],
) -> None:
    """Reject completion when the investigation lease has expired."""
    session_factory, task_id, _ = investigation_task

    async def processor() -> None:
        # Simulate lease expiration immediately before completion.
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(InvestigationTask)
                    .where(InvestigationTask.id == task_id)
                    .values(lease_expires_at=(datetime.now(UTC) - timedelta(days=1)))
                )

    with pytest.raises(runner_module.LeaseLostError):
        await runner_module.execute_investigation_task(
            session_factory=session_factory,
            task_id=task_id,
            processor=processor,
        )

    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.lease_expires_at is not None
        assert task.lease_expires_at < datetime.now(UTC)


async def test_lease_loss_cancels_processor(
    investigation_task: tuple[async_sessionmaker[AsyncSession], str, AsyncEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel processing when lease maintenance loses ownership."""
    session_factory, task_id, _ = investigation_task

    processor_started = asyncio.Event()
    processor_cancelled = asyncio.Event()

    async def processor() -> None:
        processor_started.set()

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            processor_cancelled.set()
            raise

    async def fake_lease(
        *,
        stop_event: asyncio.Event,
        **_: object,
    ) -> None:
        await processor_started.wait()

        assert not stop_event.is_set()

        raise runner_module.LeaseLostError("Simulated lease ownership loss")

    monkeypatch.setattr(
        runner_module,
        "maintain_investigation_lease",
        fake_lease,
    )

    with pytest.raises(ExceptionGroup) as exc_info:
        await asyncio.wait_for(
            runner_module.execute_investigation_task(
                session_factory=session_factory,
                task_id=task_id,
                processor=processor,
            ),
            timeout=5,
        )

    assert any(
        isinstance(exc, runner_module.LeaseLostError)
        for exc in exc_info.value.exceptions
    )

    assert processor_cancelled.is_set()

    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"


async def test_final_commit_failure_does_not_report_success(
    investigation_task: tuple[async_sessionmaker[AsyncSession], str, AsyncEngine],
) -> None:
    """Do not report success when the final transaction cannot commit."""
    session_factory, task_id, engine = investigation_task

    commit_count = 0

    def reject_second_commit(_connection: object) -> None:
        nonlocal commit_count
        commit_count += 1

        # First commit: claim the task.
        # Second commit: persist the succeeded status.
        if commit_count == 2:
            raise RuntimeError("Simulated commit failure")

    async def processor() -> None:
        return

    event.listen(
        engine.sync_engine,
        "commit",
        reject_second_commit,
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="Simulated commit failure",
        ):
            await runner_module.execute_investigation_task(
                session_factory=session_factory,
                task_id=task_id,
                processor=processor,
            )
    finally:
        event.remove(
            engine.sync_engine,
            "commit",
            reject_second_commit,
        )

    assert commit_count == 2

    # The failed final commit must not persist succeeded.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.claim_token is not None
