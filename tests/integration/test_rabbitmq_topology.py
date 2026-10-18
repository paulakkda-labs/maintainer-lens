"""Integration tests for RabbitMQ retry and dead-letter routing."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractRobustExchange,
    AbstractRobustQueue,
)

from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.core.config import get_settings
from maintainer_lens.messaging.topology import (
    DEAD_ROUTING_KEY,
    RETRY_DELAY_MS,
    RETRY_ROUTING_KEY,
    ROUTING_KEY,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_RABBITMQ_INTEGRATION") != "1",
        reason="RabbitMQ integration tests are disabled",
    ),
]


async def wait_for_message(
    queue: AbstractRobustQueue,
    timeout_seconds: float = 5.0,
) -> AbstractIncomingMessage:
    """Poll a queue until a message arrives or the deadline expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    while loop.time() < deadline:
        message = await queue.get(fail=False)

        if message is not None:
            return message

        await asyncio.sleep(0.05)

    pytest.fail(f"No message received from queue: {queue.name}")


@pytest.fixture
async def rabbitmq_test_topology() -> AsyncIterator[
    tuple[
        AbstractRobustExchange,
        AbstractRobustQueue,
        AbstractRobustQueue,
        AbstractRobustQueue,
    ]
]:
    """Create an isolated RabbitMQ topology for routing tests."""
    settings = get_settings()

    connection = await create_rabbitmq_connection(
        settings.rabbitmq_url.get_secret_value()
    )

    try:
        channel = await connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )

        prefix = f"maintainer_lens.test.{uuid4().hex}"

        exchange = await channel.declare_exchange(
            f"{prefix}.exchange",
            type=aio_pika.ExchangeType.DIRECT,
            auto_delete=True,
        )

        # Isolated pending queue.
        pending_queue = await channel.declare_queue(
            f"{prefix}.pending",
            exclusive=True,
            auto_delete=True,
            robust=False,
        )

        await pending_queue.bind(
            exchange,
            routing_key=ROUTING_KEY,
        )

        # Isolated retry queue with the same production TTL configuration.
        retry_queue = await channel.declare_queue(
            f"{prefix}.retry",
            exclusive=True,
            auto_delete=True,
            robust=False,
            arguments={
                "x-message-ttl": RETRY_DELAY_MS,
                "x-dead-letter-exchange": exchange.name,
                "x-dead-letter-routing-key": ROUTING_KEY,
            },
        )

        await retry_queue.bind(
            exchange,
            routing_key=RETRY_ROUTING_KEY,
        )

        # Isolated terminal dead-letter queue.
        dead_queue = await channel.declare_queue(
            f"{prefix}.dead",
            exclusive=True,
            auto_delete=True,
            robust=False,
        )

        await dead_queue.bind(
            exchange,
            routing_key=DEAD_ROUTING_KEY,
        )

        yield exchange, pending_queue, retry_queue, dead_queue

    finally:
        # Exclusive queues are removed when the connection closes.
        await connection.close()


async def test_retry_message_returns_to_pending_queue(
    rabbitmq_test_topology: tuple[
        AbstractRobustExchange,
        AbstractRobustQueue,
        AbstractRobustQueue,
        AbstractRobustQueue,
    ],
) -> None:
    """Route an expired retry message back to the pending queue."""
    exchange, pending_queue, retry_queue, _ = rabbitmq_test_topology

    message_id = str(uuid4())
    body = b"investigation-retry-test"

    message = aio_pika.Message(
        body=body,
        message_id=message_id,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        expiration=timedelta(milliseconds=300),
    )

    # Publish to the retry queue.
    await exchange.publish(
        message,
        routing_key=RETRY_ROUTING_KEY,
        mandatory=True,
    )

    # After expiration, RabbitMQ should dead-letter it to pending.
    received = await wait_for_message(pending_queue)

    assert received.body == body
    assert received.message_id == message_id

    # Verify that RabbitMQ marked the message as expired.
    assert received.headers.get("x-first-death-reason") == "expired"
    assert received.headers.get("x-first-death-queue") == retry_queue.name

    await received.ack()

    # The expired message should no longer remain in retry.
    assert await retry_queue.get(fail=False) is None


async def test_dead_letter_message_routes_to_dead_queue(
    rabbitmq_test_topology: tuple[
        AbstractRobustExchange,
        AbstractRobustQueue,
        AbstractRobustQueue,
        AbstractRobustQueue,
    ],
) -> None:
    """Route terminal messages to the dead-letter queue."""
    exchange, pending_queue, retry_queue, dead_queue = rabbitmq_test_topology

    message_id = str(uuid4())
    body = b"investigation-dead-test"

    message = aio_pika.Message(
        body=body,
        message_id=message_id,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )

    await exchange.publish(
        message,
        routing_key=DEAD_ROUTING_KEY,
        mandatory=True,
    )

    received = await wait_for_message(dead_queue)

    assert received.body == body
    assert received.message_id == message_id

    await received.ack()

    # Dead-letter messages must not enter pending or retry.
    assert await pending_queue.get(fail=False) is None
    assert await retry_queue.get(fail=False) is None
