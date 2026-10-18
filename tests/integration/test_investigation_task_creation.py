"""PostgreSQL integration tests for investigation task creation and rollback."""

import os

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_tasks import create_investigation_task
from maintainer_lens.db.models.investigation import InvestigationTask

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


async def test_create_investigation_task_persists_pending_task(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Persist a new investigation task with the pending status."""
    async with session_factory() as session:
        async with session.begin():
            task = await create_investigation_task(
                session=session,
                task_id=task_id,
                repository="fastapi/fastapi",
                issue_number=123,
            )

            assert task.id == task_id

    # Verify the committed data using a separate session.
    async with session_factory() as session:
        saved_task = await session.get(InvestigationTask, task_id)

        assert saved_task is not None
        assert saved_task.repository == "fastapi/fastapi"
        assert saved_task.issue_number == 123
        assert saved_task.status == "pending"
        assert saved_task.created_at is not None
        assert saved_task.updated_at is not None


async def test_create_investigation_task_rejects_invalid_issue_number(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Reject investigation tasks with non-positive issue numbers."""
    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            async with session.begin():
                await create_investigation_task(
                    session=session,
                    task_id=task_id,
                    repository="fastapi/fastapi",
                    issue_number=0,
                )

    async with session_factory() as session:
        saved_task = await session.get(InvestigationTask, task_id)

        assert saved_task is None


async def test_create_investigation_task_rolls_back_on_failure(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> None:
    """Roll back an inserted task when its transaction fails."""
    with pytest.raises(RuntimeError, match="Simulated transaction failure"):
        async with session_factory() as session:
            async with session.begin():
                await create_investigation_task(
                    session=session,
                    task_id=task_id,
                    repository="fastapi/fastapi",
                    issue_number=123,
                )

                raise RuntimeError("Simulated transaction failure")

    # A successful flush must not survive a transaction rollback.
    async with session_factory() as session:
        saved_task = await session.get(InvestigationTask, task_id)

        assert saved_task is None
