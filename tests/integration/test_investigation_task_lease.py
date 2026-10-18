"""PostgreSQL integration tests for lease renewal and retry release."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    create_investigation_task,
    mark_investigation_succeeded,
    release_investigation_task_for_retry,
    renew_investigation_lease,
)
from maintainer_lens.db.models.investigation import InvestigationTask

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


async def test_renew_investigation_lease_requires_valid_claim(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Renew only active leases owned by the current worker."""
    # Create a pending investigation task.
    async with session_factory() as session:
        async with session.begin():
            await create_investigation_task(
                session=session,
                task_id=task_id,
                repository="fastapi/fastapi",
                issue_number=123,
            )

    # A pending task cannot renew a lease.
    async with session_factory() as session:
        async with session.begin():
            result = await renew_investigation_lease(
                session,
                task_id,
                "invalid-token",
            )

            assert result is False

    # Claim the task.
    async with session_factory() as session:
        async with session.begin():
            first_token = await claim_investigation_task(
                session,
                task_id,
            )

    assert isinstance(first_token, str)

    # An incorrect token cannot renew an active lease.
    async with session_factory() as session:
        async with session.begin():
            result = await renew_investigation_lease(
                session,
                task_id,
                "invalid-token",
            )

            assert result is False

    # Shorten the active lease to one minute.
    short_expiry = datetime.now(UTC) + timedelta(minutes=1)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(lease_expires_at=short_expiry)
            )

    # The current worker can renew its active lease.
    async with session_factory() as session:
        async with session.begin():
            result = await renew_investigation_lease(
                session,
                task_id,
                first_token,
            )

            assert result is True

    # Renewal extends the lease without changing ownership.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.claim_token == first_token
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > short_expiry + timedelta(minutes=10)

    # Expire the lease.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(days=1))
            )

    # An expired lease cannot be renewed.
    async with session_factory() as session:
        async with session.begin():
            result = await renew_investigation_lease(
                session,
                task_id,
                first_token,
            )

            assert result is False

    # Another worker reclaims the expired task.
    async with session_factory() as session:
        async with session.begin():
            second_token = await claim_investigation_task(
                session,
                task_id,
            )

    assert isinstance(second_token, str)
    assert second_token != first_token

    # The previous worker can no longer renew the task.
    async with session_factory() as session:
        async with session.begin():
            result = await renew_investigation_lease(
                session,
                task_id,
                first_token,
            )

            assert result is False

    # The new owner can renew the task.
    async with session_factory() as session:
        async with session.begin():
            result = await renew_investigation_lease(
                session,
                task_id,
                second_token,
            )

            assert result is True

    # Complete the task.
    async with session_factory() as session:
        async with session.begin():
            result = await mark_investigation_succeeded(
                session,
                task_id,
                second_token,
            )

            assert result is True

    # Completed tasks cannot renew their leases.
    async with session_factory() as session:
        async with session.begin():
            result = await renew_investigation_lease(
                session,
                task_id,
                second_token,
            )

            assert result is False

    # Verify the final persisted state.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "succeeded"
        assert task.claim_token is None
        assert task.lease_expires_at is None


async def test_release_investigation_task_for_retry(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Allow only the current lease owner to release a task for retry."""
    # Create a pending investigation task.
    async with session_factory() as session:
        async with session.begin():
            await create_investigation_task(
                session=session,
                task_id=task_id,
                repository="fastapi/fastapi",
                issue_number=123,
            )

    # A pending task cannot be released.
    async with session_factory() as session:
        async with session.begin():
            released = await release_investigation_task_for_retry(
                session=session,
                task_id=task_id,
                claim_token="invalid-token",
            )

            assert released is False

    # Worker A claims the task.
    async with session_factory() as session:
        async with session.begin():
            first_token = await claim_investigation_task(
                session=session,
                task_id=task_id,
            )

    assert isinstance(first_token, str)

    # An incorrect token cannot release an active task.
    async with session_factory() as session:
        async with session.begin():
            released = await release_investigation_task_for_retry(
                session=session,
                task_id=task_id,
                claim_token="invalid-token",
            )

            assert released is False

    # Simulate Worker A's lease expiring.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(lease_expires_at=(datetime.now(UTC) - timedelta(days=1)))
            )

    # Worker A cannot release an expired lease.
    async with session_factory() as session:
        async with session.begin():
            released = await release_investigation_task_for_retry(
                session=session,
                task_id=task_id,
                claim_token=first_token,
            )

            assert released is False

    # Worker B reclaims the expired task.
    async with session_factory() as session:
        async with session.begin():
            second_token = await claim_investigation_task(
                session=session,
                task_id=task_id,
            )

    assert isinstance(second_token, str)
    assert second_token != first_token

    # Worker A cannot release Worker B's task.
    async with session_factory() as session:
        async with session.begin():
            released = await release_investigation_task_for_retry(
                session=session,
                task_id=task_id,
                claim_token=first_token,
            )

            assert released is False

    # Worker B releases its own active claim.
    async with session_factory() as session:
        async with session.begin():
            released = await release_investigation_task_for_retry(
                session=session,
                task_id=task_id,
                claim_token=second_token,
            )

            assert released is True

    # Verify that the release was committed.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "pending"
        assert task.claim_token is None
        assert task.lease_expires_at is None

    # A released task can be claimed again.
    async with session_factory() as session:
        async with session.begin():
            third_token = await claim_investigation_task(
                session=session,
                task_id=task_id,
            )

    assert isinstance(third_token, str)
    assert third_token not in (first_token, second_token)

    # Verify the new owner's persisted claim.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == "running"
        assert task.claim_token == third_token
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > datetime.now(UTC)
