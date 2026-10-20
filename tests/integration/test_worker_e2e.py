"""End-to-end tests for the investigation worker."""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractRobustChannel,
    AbstractRobustExchange,
    AbstractRobustQueue,
)
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.core.config import get_settings
from maintainer_lens.db.investigation_tasks import (
    create_investigation_task,
    get_investigation_task_status,
)
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.investigation_runner import RetryableInvestigationError
from maintainer_lens.messaging.consumer import consume_investigations
from maintainer_lens.messaging.publisher import publish_investigation
from maintainer_lens.messaging.schemas import (
    RETRY_ATTEMPTS_HEADER,
    InvestigationRequestedMessage,
)
from maintainer_lens.messaging.topology import (
    DEAD_ROUTING_KEY,
    RETRY_ROUTING_KEY,
    ROUTING_KEY,
)
from maintainer_lens.worker import build_investigation_message_handler

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_WORKER_E2E") != "1",
        reason="Worker end-to-end tests are disabled",
    ),
]


@dataclass
class BrokerResources:
    """Isolated RabbitMQ resources for one integration test."""

    exchange: AbstractRobustExchange
    pending: AbstractRobustQueue
    dead: AbstractRobustQueue
    consumer_channel: AbstractRobustChannel
    publisher_channel: AbstractRobustChannel


@pytest.fixture
async def test_broker() -> AsyncIterator[BrokerResources]:
    """Create isolated RabbitMQ queues with a short retry delay."""
    settings = get_settings()

    connection = await aio_pika.connect_robust(settings.rabbitmq_url.get_secret_value())

    try:
        consumer_channel = await connection.channel(
            publisher_confirms=False,
        )
        publisher_channel = await connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )

        await consumer_channel.set_qos(prefetch_count=1)

        prefix = f"ml.e2e.{uuid4().hex}"

        exchange = await publisher_channel.declare_exchange(
            prefix,
            type=aio_pika.ExchangeType.DIRECT,
            durable=False,
            auto_delete=True,
        )

        pending = await consumer_channel.declare_queue(
            f"{prefix}.pending",
            durable=False,
            exclusive=True,
        )
        await pending.bind(exchange, routing_key=ROUTING_KEY)

        retry = await publisher_channel.declare_queue(
            f"{prefix}.retry",
            durable=False,
            exclusive=True,
            arguments={
                "x-message-ttl": 250,
                "x-dead-letter-exchange": exchange.name,
                "x-dead-letter-routing-key": ROUTING_KEY,
            },
        )
        await retry.bind(exchange, routing_key=RETRY_ROUTING_KEY)

        dead = await publisher_channel.declare_queue(
            f"{prefix}.dead",
            durable=False,
            exclusive=True,
        )
        await dead.bind(exchange, routing_key=DEAD_ROUTING_KEY)

        yield BrokerResources(
            exchange=exchange,
            pending=pending,
            dead=dead,
            consumer_channel=consumer_channel,
            publisher_channel=publisher_channel,
        )

    finally:
        await connection.close()


@pytest.fixture
async def investigation_request(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[InvestigationRequestedMessage]:
    """Create and clean up one real PostgreSQL investigation task."""
    request = InvestigationRequestedMessage(
        task_id=f"e2e-{uuid4().hex}",
        repository="fastapi/fastapi",
        issue_number=123,
    )

    async with session_factory() as session:
        async with session.begin():
            await create_investigation_task(
                session=session,
                task_id=request.task_id,
                repository=request.repository,
                issue_number=request.issue_number,
            )

    try:
        yield request
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(InvestigationTask).where(
                        InvestigationTask.id == request.task_id
                    )
                )


async def read_task_status(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> str | None:
    """Read the persisted investigation status."""
    async with session_factory() as session:
        return await get_investigation_task_status(
            session=session,
            task_id=task_id,
        )


async def publish_test_request(
    broker: BrokerResources,
    request: InvestigationRequestedMessage,
    retry_attempts: int = 0,
) -> None:
    """Publish an investigation with optional retry metadata."""
    if retry_attempts == 0:
        await publish_investigation(
            exchange=broker.exchange,
            task_id=request.task_id,
            repository=request.repository,
            issue_number=request.issue_number,
        )
        return

    message = aio_pika.Message(
        body=request.model_dump_json().encode(),
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type="application/json",
        message_id=request.task_id,
        headers={RETRY_ATTEMPTS_HEADER: retry_attempts},
    )

    await broker.exchange.publish(
        message,
        routing_key=ROUTING_KEY,
        mandatory=True,
    )


def start_test_consumer(
    broker: BrokerResources,
    session_factory: async_sessionmaker[AsyncSession],
    investigate: Callable[
        [InvestigationRequestedMessage, str],
        Awaitable[None],
    ],
) -> tuple[asyncio.Task[None], list[AbstractIncomingMessage]]:
    """Start the real consumer and record delivered messages."""
    handler = build_investigation_message_handler(
        session_factory=session_factory,
        exchange=broker.exchange,
        investigate=investigate,
    )

    deliveries: list[AbstractIncomingMessage] = []

    async def recording_handler(message: AbstractIncomingMessage) -> None:
        deliveries.append(message)
        await handler(message)

    task = asyncio.create_task(
        consume_investigations(
            queue=broker.pending,
            handler=recording_handler,
        )
    )

    return task, deliveries


async def wait_for_acknowledgements(
    task: asyncio.Task[None],
    deliveries: list[AbstractIncomingMessage],
    expected_count: int,
) -> None:
    """Wait until the consumer acknowledges the expected deliveries."""
    async with asyncio.timeout(15):
        while True:
            if task.done():
                await task
                raise AssertionError("Consumer stopped unexpectedly")

            if len(deliveries) >= expected_count and all(
                message.processed for message in deliveries[:expected_count]
            ):
                return

            await asyncio.sleep(0.02)


async def stop_test_consumer(task: asyncio.Task[None]) -> None:
    """Cancel the test consumer after its assertions complete."""
    if not task.done():
        task.cancel()

    with suppress(asyncio.CancelledError):
        await task


async def test_successful_investigation_e2e(
    test_broker: BrokerResources,
    investigation_request: InvestigationRequestedMessage,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Complete and acknowledge a successful investigation."""
    calls: list[InvestigationRequestedMessage] = []

    async def investigate(
        request: InvestigationRequestedMessage,
        _claim_token: str,
    ) -> None:
        calls.append(request)

    task, deliveries = start_test_consumer(
        test_broker,
        session_factory,
        investigate,
    )

    try:
        await publish_test_request(test_broker, investigation_request)

        await wait_for_acknowledgements(task, deliveries, 1)

        status = await read_task_status(
            session_factory,
            investigation_request.task_id,
        )

        assert status == "succeeded"
        assert calls == [investigation_request]
        assert deliveries[0].processed is True

    finally:
        await stop_test_consumer(task)


async def test_retry_then_success_e2e(
    test_broker: BrokerResources,
    investigation_request: InvestigationRequestedMessage,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Retry a temporary failure and complete on the second delivery."""
    attempts = 0

    async def investigate(
        request: InvestigationRequestedMessage,
        _claim_token: str,
    ) -> None:
        nonlocal attempts

        attempts += 1

        if attempts == 1:
            raise RetryableInvestigationError("Temporary GitHub failure")

    task, deliveries = start_test_consumer(
        test_broker,
        session_factory,
        investigate,
    )

    try:
        await publish_test_request(test_broker, investigation_request)

        await wait_for_acknowledgements(task, deliveries, 2)

        status = await read_task_status(
            session_factory,
            investigation_request.task_id,
        )

        assert status == "succeeded"
        assert attempts == 2
        assert deliveries[0].processed is True
        assert deliveries[1].processed is True
        assert deliveries[1].headers[RETRY_ATTEMPTS_HEADER] == 1

    finally:
        await stop_test_consumer(task)


async def test_final_failure_dead_letters_e2e(
    test_broker: BrokerResources,
    investigation_request: InvestigationRequestedMessage,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Persist final failure and publish its message to the dead queue."""

    async def investigate(
        request: InvestigationRequestedMessage,
        _claim_token: str,
    ) -> None:
        raise RetryableInvestigationError("GitHub remains unavailable")

    task, deliveries = start_test_consumer(
        test_broker,
        session_factory,
        investigate,
    )

    try:
        await publish_test_request(
            test_broker,
            investigation_request,
            retry_attempts=3,
        )

        await wait_for_acknowledgements(task, deliveries, 1)

        status = await read_task_status(
            session_factory,
            investigation_request.task_id,
        )

        assert status == "failed"
        assert deliveries[0].processed is True

        dead_message = await test_broker.dead.get(
            timeout=5,
            fail=True,
        )

        assert dead_message.body == investigation_request.model_dump_json().encode()
        assert dead_message.headers[RETRY_ATTEMPTS_HEADER] == 3

        await dead_message.ack()

    finally:
        await stop_test_consumer(task)


async def test_publish_failure_preserves_original_message_e2e(
    test_broker: BrokerResources,
    investigation_request: InvestigationRequestedMessage,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Keep the original delivery unacked when retry publication fails."""

    async def investigate(
        request: InvestigationRequestedMessage,
        _claim_token: str,
    ) -> None:
        raise RetryableInvestigationError("Temporary GitHub failure")

    class FailingExchange:
        async def publish(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("Simulated publish failure")

    handler = build_investigation_message_handler(
        session_factory=session_factory,
        exchange=FailingExchange(),
        investigate=investigate,
    )

    deliveries: list[AbstractIncomingMessage] = []

    async def recording_handler(message: AbstractIncomingMessage) -> None:
        deliveries.append(message)
        await handler(message)

    task = asyncio.create_task(
        consume_investigations(
            queue=test_broker.pending,
            handler=recording_handler,
        )
    )

    try:
        await publish_test_request(
            test_broker,
            investigation_request,
        )

        with pytest.raises(
            RuntimeError,
            match="Simulated publish failure",
        ):
            await asyncio.wait_for(task, timeout=15)

        assert len(deliveries) == 1
        assert deliveries[0].processed is False

        status = await read_task_status(
            session_factory,
            investigation_request.task_id,
        )

        assert status == "pending"

        await test_broker.consumer_channel.close()

        pending = await test_broker.publisher_channel.get_queue(
            test_broker.pending.name,
            ensure=True,
        )

        redelivered_message = await pending.get(
            timeout=5,
            fail=True,
        )

        assert redelivered_message.redelivered is True
        assert redelivered_message.body == deliveries[0].body

        await redelivered_message.ack()

    finally:
        if not task.done():
            await stop_test_consumer(task)
