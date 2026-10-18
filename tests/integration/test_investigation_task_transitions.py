"""PostgreSQL integration tests for task state transitions and status."""

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    create_investigation_task,
    get_investigation_task_status,
    mark_investigation_failed,
    mark_investigation_succeeded,
)
from maintainer_lens.db.models.investigation import InvestigationTask

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


@pytest.mark.parametrize(
    ("transition", "expected_status"),
    [
        pytest.param(
            mark_investigation_succeeded,
            "succeeded",
            id="succeeded",
        ),
        pytest.param(
            mark_investigation_failed,
            "failed",
            id="failed",
        ),
    ],
)
async def test_investigation_status_transition_requires_valid_claim(
    transition: Callable[[AsyncSession, str, str], Awaitable[bool]],
    expected_status: str,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Allow final status transitions only with a valid active claim."""
    # Create a pending investigation task.
    async with session_factory() as session:
        async with session.begin():
            await create_investigation_task(
                session=session,
                task_id=task_id,
                repository="fastapi/fastapi",
                issue_number=123,
            )

    # Pending tasks cannot transition directly to a final status.
    async with session_factory() as session:
        async with session.begin():
            result = await transition(
                session,
                task_id,
                "invalid-token",
            )
            assert result is False

    # Claim the task for the first time.
    async with session_factory() as session:
        async with session.begin():
            first_token = await claim_investigation_task(
                session,
                task_id,
            )

    assert isinstance(first_token, str)

    # An incorrect token cannot complete the task.
    async with session_factory() as session:
        async with session.begin():
            result = await transition(
                session,
                task_id,
                "invalid-token",
            )
            assert result is False

    # Simulate lease expiration.
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(days=1))
            )

    # An expired claim cannot complete the task.
    async with session_factory() as session:
        async with session.begin():
            result = await transition(
                session,
                task_id,
                first_token,
            )
            assert result is False

    # Reclaim the expired task with a new token.
    async with session_factory() as session:
        async with session.begin():
            second_token = await claim_investigation_task(
                session,
                task_id,
            )

    assert isinstance(second_token, str)
    assert second_token != first_token

    # The previous worker must not modify the reclaimed task.
    async with session_factory() as session:
        async with session.begin():
            result = await transition(
                session,
                task_id,
                first_token,
            )
            assert result is False

    # The current worker can complete the task.
    async with session_factory() as session:
        async with session.begin():
            result = await transition(
                session,
                task_id,
                second_token,
            )
            assert result is True

    # Verify the final status and cleared lease.
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)

        assert task is not None
        assert task.status == expected_status
        assert task.claim_token is None
        assert task.lease_expires_at is None

    # A completed task cannot transition again.
    async with session_factory() as session:
        async with session.begin():
            result = await transition(
                session,
                task_id,
                second_token,
            )
            assert result is False


@pytest.mark.parametrize(
    ("transition", "expected_status"),
    [
        pytest.param(
            mark_investigation_succeeded,
            "succeeded",
            id="succeeded",
        ),
        pytest.param(
            mark_investigation_failed,
            "failed",
            id="failed",
        ),
    ],
)
async def test_get_investigation_task_status_tracks_lifecycle(
    transition: Callable[[AsyncSession, str, str], Awaitable[bool]],
    expected_status: str,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Return the persisted status throughout an investigation lifecycle."""
    # A nonexistent task has no status.
    async with session_factory() as session:
        status = await get_investigation_task_status(session, task_id)
        assert status is None

    # Create a pending task.
    async with session_factory() as session:
        async with session.begin():
            await create_investigation_task(
                session=session,
                task_id=task_id,
                repository="fastapi/fastapi",
                issue_number=123,
            )

    async with session_factory() as session:
        status = await get_investigation_task_status(session, task_id)
        assert status == "pending"

    # Claim the task.
    async with session_factory() as session:
        async with session.begin():
            claim_token = await claim_investigation_task(
                session=session,
                task_id=task_id,
            )

    assert isinstance(claim_token, str)

    async with session_factory() as session:
        status = await get_investigation_task_status(session, task_id)
        assert status == "running"

    # Transition to the expected final status.
    async with session_factory() as session:
        async with session.begin():
            completed = await transition(
                session,
                task_id,
                claim_token,
            )
            assert completed is True

    async with session_factory() as session:
        status = await get_investigation_task_status(session, task_id)
        assert status == expected_status
