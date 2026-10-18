"""PostgreSQL integration tests for concurrent task claiming and reclamation."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    create_investigation_task,
)
from maintainer_lens.db.models.investigation import InvestigationTask

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


async def test_concurrent_claim_allows_only_one_worker(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Allow only one concurrent worker to claim a pending task."""
    # Create a pending investigation task.
    async with session_factory() as session:
        async with session.begin():
            session.add(
                InvestigationTask(
                    id=task_id,
                    repository="fastapi/fastapi",
                    issue_number=123,
                )
            )

    # Synchronize both workers before attempting to claim the task.
    barrier = asyncio.Barrier(2)

    async def attempt_claim() -> str | None:
        async with session_factory() as session:
            async with session.begin():
                await barrier.wait()

                return await claim_investigation_task(
                    session=session,
                    task_id=task_id,
                )

    # Simulate two workers competing for the same task.
    results = await asyncio.wait_for(
        asyncio.gather(
            attempt_claim(),
            attempt_claim(),
        ),
        timeout=10,
    )

    assert sum(token is not None for token in results) == 1

    # Verify the final persisted task state.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"


async def test_claim_investigation_task_respects_lease_expiration(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Reject active leases and allow expired tasks to be reclaimed."""
    # Create a pending investigation task.
    async with session_factory() as session:
        async with session.begin():
            await create_investigation_task(
                session=session,
                task_id=task_id,
                repository="fastapi/fastapi",
                issue_number=123,
            )

    # Claim the task for the first time.
    async with session_factory() as session:
        async with session.begin():
            first_token = await claim_investigation_task(session, task_id)

    assert isinstance(first_token, str)

    # Verify that the task has an active lease.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.claim_token == first_token
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > datetime.now(UTC)

    # An active lease prevents another claim.
    async with session_factory() as session:
        async with session.begin():
            result = await claim_investigation_task(session, task_id)

    assert result is None

    # Simulate lease expiration without waiting.
    expired_at = datetime.now(UTC) - timedelta(days=1)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(lease_expires_at=expired_at)
            )

    # An expired task can be reclaimed.
    async with session_factory() as session:
        async with session.begin():
            second_token = await claim_investigation_task(session, task_id)

    assert isinstance(second_token, str)
    assert second_token != first_token

    # Verify the new lease and claim token.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.claim_token == second_token
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > datetime.now(UTC)


async def test_concurrent_reclaim_allows_only_one_worker(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Allow only one worker to reclaim an expired investigation task."""
    # Create and claim the investigation task.
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
            original_token = await claim_investigation_task(session, task_id)

    assert isinstance(original_token, str)

    # Expire the existing lease.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(days=1))
            )

    barrier = asyncio.Barrier(2)

    async def attempt_reclaim() -> str | None:
        async with session_factory() as session:
            async with session.begin():
                await barrier.wait()
                return await claim_investigation_task(session, task_id)

    # Two workers compete to reclaim the expired task.
    results = await asyncio.wait_for(
        asyncio.gather(
            attempt_reclaim(),
            attempt_reclaim(),
        ),
        timeout=10,
    )

    successful_tokens = [token for token in results if token is not None]

    assert len(successful_tokens) == 1

    new_token = successful_tokens[0]
    assert new_token != original_token

    # Verify that the winning worker owns the current lease.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.claim_token == new_token
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > datetime.now(UTC)
