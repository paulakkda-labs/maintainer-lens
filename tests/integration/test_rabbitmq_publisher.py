"""Integration tests for RabbitMQ investigation message publishing."""

import json
import os
from contextlib import AsyncExitStack
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.exceptions import PublishError

from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.core.config import get_settings
from maintainer_lens.messaging.publisher import publish_investigation
from maintainer_lens.messaging.topology import ROUTING_KEY


@pytest.mark.anyio
@pytest.mark.skipif(
    os.getenv("RUN_RABBITMQ_INTEGRATION") != "1",
    reason="RabbitMQ integration tests are disabled",
)
async def test_publish_investigation_routes_message_to_queue() -> None:
    """Verify that RabbitMQ routes a published investigation message."""
    settings = get_settings()

    task_id = f"test-task-{uuid4().hex}"
    repository = "fastapi/fastapi"
    issue_number = 123

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
            f"maintainer_lens.tests.{uuid4().hex}",
            type=aio_pika.ExchangeType.DIRECT,
            auto_delete=True,
            robust=False,
        )

        queue = await channel.declare_queue(
            exclusive=True,
            auto_delete=True,
            robust=False,
        )

        await queue.bind(
            exchange,
            routing_key=ROUTING_KEY,
            robust=False,
        )

        await publish_investigation(
            exchange=exchange,
            task_id=task_id,
            repository=repository,
            issue_number=issue_number,
        )

        received = await queue.get(timeout=5)

        async with received.process(requeue=False):
            assert received.message_id == task_id
            assert received.content_type == "application/json"
            assert received.delivery_mode == aio_pika.DeliveryMode.PERSISTENT

            payload = json.loads(received.body.decode("utf-8"))

            assert payload == {
                "task_id": task_id,
                "repository": repository,
                "issue_number": issue_number,
            }


@pytest.mark.anyio
@pytest.mark.skipif(
    os.getenv("RUN_RABBITMQ_INTEGRATION") != "1",
    reason="RabbitMQ integration tests are disabled",
)
async def test_publish_investigation_raises_when_message_is_unroutable() -> None:
    """Raise PublishError when no queue matches the routing key."""
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
            f"maintainer_lens.tests.unroutable.{uuid4().hex}",
            type=aio_pika.ExchangeType.DIRECT,
            durable=False,
            robust=False,
        )
        stack.push_async_callback(exchange.delete)

        with pytest.raises(PublishError):
            await publish_investigation(
                exchange=exchange,
                task_id=f"test-task-{uuid4().hex}",
                repository="fastapi/fastapi",
                issue_number=123,
            )
