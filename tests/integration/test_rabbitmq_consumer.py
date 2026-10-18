"""Integration tests for RabbitMQ investigation message consumption."""

import asyncio
import os
from contextlib import AsyncExitStack
from uuid import uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractIncomingMessage

from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.core.config import get_settings
from maintainer_lens.messaging.consumer import consume_investigations


@pytest.mark.anyio
@pytest.mark.skipif(
    os.getenv("RUN_RABBITMQ_INTEGRATION") != "1",
    reason="RabbitMQ integration tests are disabled",
)
async def test_consumer_requeues_unacknowledged_message_on_channel_close() -> None:
    """Verify successful ACK and redelivery after a processing failure."""
    settings = get_settings()
    processed: list[bytes] = []

    async with AsyncExitStack() as stack:
        connection = await create_rabbitmq_connection(
            settings.rabbitmq_url.get_secret_value()
        )
        stack.push_async_callback(connection.close)

        async with AsyncExitStack() as consumer_stack:
            channel = await connection.channel(publisher_confirms=True)
            consumer_stack.push_async_callback(channel.close)

            await channel.set_qos(prefetch_count=1)

            queue = await channel.declare_queue(
                f"maintainer_lens.tests.consumer.{uuid4().hex}",
                exclusive=True,
                auto_delete=False,
                robust=False,
            )
            queue_name = queue.name

            for body in (b"success", b"failure"):
                await channel.default_exchange.publish(
                    aio_pika.Message(body=body),
                    routing_key=queue_name,
                )

            async def handler(message: AbstractIncomingMessage) -> None:
                processed.append(message.body)

                if message.body == b"failure":
                    raise RuntimeError("Simulated processing failure")

            with pytest.raises(
                RuntimeError,
                match="Simulated processing failure",
            ):
                await asyncio.wait_for(
                    consume_investigations(queue, handler),
                    timeout=5,
                )

            assert processed == [b"success", b"failure"]

        # Closing the consumer channel requeues unacknowledged deliveries.
        verification_channel = await connection.channel()
        stack.push_async_callback(verification_channel.close)

        verification_queue = await verification_channel.get_queue(
            queue_name,
            ensure=True,
        )

        redelivered = await verification_queue.get(timeout=5)

        assert redelivered.body == b"failure"
        assert redelivered.redelivered is True

        await redelivered.ack()
