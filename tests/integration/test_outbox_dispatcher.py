"""Integration tests for outbox dispatch and RabbitMQ publishing."""

import asyncio
import contextlib
import os
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import aio_pika
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import maintainer_lens.services.investigation_submission as submission_module
import maintainer_lens.services.outbox_dispatcher as dispatcher_module
from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.outbox import claim_next_outbox_event
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.messaging.topology import ROUTING_KEY
from maintainer_lens.outbox_dispatcher import run_dispatcher_loop

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


@pytest.mark.anyio
async def test_outbox_dispatch_success(
    investigation_task: tuple[
        async_sessionmaker[AsyncSession],
        str,
        AsyncEngine,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publish an event and persist the publisher confirmation."""

    session_factory, _, _ = investigation_task

    message = await submission_module.create_investigation_submission(
        session_factory,
        repository="encode/httpx",
        issue_number=3712,
    )

    publish = AsyncMock()
    exchange = Mock()

    monkeypatch.setattr(
        dispatcher_module,
        "publish_investigation",
        publish,
    )

    try:
        dispatched = await dispatcher_module.dispatch_one_outbox_event(
            session_factory,
            exchange,
        )

        assert dispatched is True

        publish.assert_awaited_once_with(
            exchange=exchange,
            task_id=message.task_id,
            repository="encode/httpx",
            issue_number=3712,
            source_index_id=None,
            auto_prepare_knowledge=False,
            response_language="en",
        )

        async with session_factory() as session:
            event = await session.scalar(
                select(InvestigationOutboxEvent).where(
                    InvestigationOutboxEvent.task_id == message.task_id
                )
            )

            assert event is not None
            assert event.published_at is not None
            assert event.delivery_token is None
            assert event.delivery_lease_expires_at is None
            assert event.publish_attempts == 1

    finally:
        async with session_factory.begin() as session:
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == message.task_id)
            )


@pytest.mark.anyio
async def test_outbox_dispatch_publish_failure(
    investigation_task: tuple[
        async_sessionmaker[AsyncSession],
        str,
        AsyncEngine,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep failed publications pending and schedule a retry."""

    session_factory, _, _ = investigation_task

    message = await submission_module.create_investigation_submission(
        session_factory,
        repository="encode/httpx",
        issue_number=3712,
    )

    publish = AsyncMock(side_effect=RuntimeError("RabbitMQ unavailable"))

    monkeypatch.setattr(
        dispatcher_module,
        "publish_investigation",
        publish,
    )

    try:
        with pytest.raises(RuntimeError, match="RabbitMQ unavailable"):
            await dispatcher_module.dispatch_one_outbox_event(
                session_factory,
                Mock(),
            )

        publish.assert_awaited_once()

        async with session_factory() as session:
            event = await session.scalar(
                select(InvestigationOutboxEvent).where(
                    InvestigationOutboxEvent.task_id == message.task_id
                )
            )

            assert event is not None
            assert event.published_at is None
            assert event.delivery_token is None
            assert event.delivery_lease_expires_at is not None
            assert event.publish_attempts == 1

        async with session_factory.begin() as session:
            assert await claim_next_outbox_event(session) is None

    finally:
        async with session_factory.begin() as session:
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == message.task_id)
            )


@pytest.mark.anyio
async def test_outbox_dispatch_confirmation_failure(
    investigation_task: tuple[
        async_sessionmaker[AsyncSession],
        str,
        AsyncEngine,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the event recoverable if publication recording fails."""

    session_factory, _, _ = investigation_task

    message = await submission_module.create_investigation_submission(
        session_factory,
        repository="encode/httpx",
        issue_number=3712,
    )

    publish = AsyncMock()

    monkeypatch.setattr(
        dispatcher_module,
        "publish_investigation",
        publish,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "mark_outbox_published",
        AsyncMock(side_effect=RuntimeError("Database confirmation failed")),
    )

    try:
        with pytest.raises(
            RuntimeError,
            match="Database confirmation failed",
        ):
            await dispatcher_module.dispatch_one_outbox_event(
                session_factory,
                Mock(),
            )

        publish.assert_awaited_once()

        async with session_factory() as session:
            event = await session.scalar(
                select(InvestigationOutboxEvent).where(
                    InvestigationOutboxEvent.task_id == message.task_id
                )
            )

            assert event is not None
            assert event.published_at is None
            assert event.delivery_token is not None
            assert event.delivery_lease_expires_at is not None
            assert event.publish_attempts == 1

    finally:
        async with session_factory.begin() as session:
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == message.task_id)
            )


@pytest.mark.skipif(
    os.getenv("RUN_WORKER_E2E") != "1",
    reason="RabbitMQ E2E tests are disabled",
)
async def test_outbox_dispatcher_delivers_real_message(
    investigation_task: tuple[
        async_sessionmaker[AsyncSession],
        str,
        AsyncEngine,
    ],
) -> None:
    """Deliver an outbox event through a real RabbitMQ exchange."""

    session_factory, _, _ = investigation_task

    request = await submission_module.create_investigation_submission(
        session_factory,
        repository="encode/httpx",
        issue_number=3712,
    )

    try:
        settings = get_settings()

        async with AsyncExitStack() as stack:
            connection = await create_rabbitmq_connection(
                settings.rabbitmq_url.get_secret_value()
            )
            stack.push_async_callback(connection.close)

            channel = await connection.channel(
                publisher_confirms=True,
                on_return_raises=True,
            )
            stack.push_async_callback(channel.close)

            exchange = await channel.declare_exchange(
                f"maintainer_lens.test.{uuid4().hex}",
                type=aio_pika.ExchangeType.DIRECT,
                auto_delete=True,
            )

            queue = await channel.declare_queue(
                "",
                exclusive=True,
                auto_delete=True,
            )
            await queue.bind(exchange, routing_key=ROUTING_KEY)

            dispatcher_task = asyncio.create_task(
                run_dispatcher_loop(session_factory, exchange)
            )

            try:
                async with queue.iterator() as messages:
                    incoming = await asyncio.wait_for(
                        anext(messages),
                        timeout=5,
                    )

                message = InvestigationRequestedMessage.model_validate_json(
                    incoming.body
                )

                assert message == request
                assert incoming.message_id == request.task_id

                await incoming.ack()

                # Publishing to RabbitMQ may finish before the database update.
                for _ in range(50):
                    async with session_factory() as session:
                        event = await session.scalar(
                            select(InvestigationOutboxEvent).where(
                                InvestigationOutboxEvent.task_id == request.task_id
                            )
                        )

                        if event is not None and event.published_at is not None:
                            assert event.delivery_token is None
                            assert event.publish_attempts == 1
                            break

                    await asyncio.sleep(0.1)
                else:
                    pytest.fail("Outbox publication was not confirmed")

            finally:
                dispatcher_task.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await dispatcher_task

    finally:
        async with session_factory.begin() as session:
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == request.task_id)
            )
