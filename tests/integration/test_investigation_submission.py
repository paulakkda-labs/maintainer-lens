"""Integration tests for atomic investigation submission and rollback."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import maintainer_lens.services.investigation_submission as submission_module
from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.messaging.schemas import INVESTIGATION_REQUESTED_EVENT_TYPE

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


@pytest.mark.anyio
async def test_submission_creates_task_and_outbox(
    investigation_task: tuple[
        async_sessionmaker[AsyncSession],
        str,
        AsyncEngine,
    ],
) -> None:
    """Create a task and its outbox event in one transaction."""

    session_factory, _, _ = investigation_task

    message = await submission_module.create_investigation_submission(
        session_factory,
        repository="encode/httpx",
        issue_number=3712,
    )

    try:
        async with session_factory() as session:
            task = await session.get(
                InvestigationTask,
                message.task_id,
            )

            event = await session.scalar(
                select(InvestigationOutboxEvent).where(
                    InvestigationOutboxEvent.task_id == message.task_id
                )
            )

            assert task is not None
            assert task.repository == "encode/httpx"
            assert task.issue_number == 3712
            assert task.status == "pending"
            assert task.claim_token is None

            assert event is not None
            assert event.event_type == INVESTIGATION_REQUESTED_EVENT_TYPE
            assert event.payload == message.model_dump(mode="json")
            assert event.published_at is None
            assert event.delivery_token is None
            assert event.publish_attempts == 0

    finally:
        async with session_factory.begin() as session:
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == message.task_id)
            )


@pytest.mark.anyio
async def test_submission_rolls_back_when_outbox_insert_fails(
    investigation_task: tuple[
        async_sessionmaker[AsyncSession],
        str,
        AsyncEngine,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roll back the task if its outbox event cannot be inserted."""

    session_factory, _, _ = investigation_task

    task_uuid = uuid4()
    task_id = str(task_uuid)

    monkeypatch.setattr(
        submission_module,
        "uuid4",
        lambda: task_uuid,
    )

    monkeypatch.setattr(
        submission_module,
        "INVESTIGATION_REQUESTED_EVENT_TYPE",
        "x" * 65,
    )

    with pytest.raises(DBAPIError, match="value too long"):
        await submission_module.create_investigation_submission(
            session_factory,
            repository="encode/httpx",
            issue_number=3712,
        )

    async with session_factory() as session:
        task = await session.get(
            InvestigationTask,
            task_id,
        )

        event_id = await session.scalar(
            select(InvestigationOutboxEvent.id).where(
                InvestigationOutboxEvent.task_id == task_id
            )
        )

    assert task is None
    assert event_id is None
