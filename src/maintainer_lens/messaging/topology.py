"""RabbitMQ topology definitions for investigation tasks."""

import aio_pika
from aio_pika.abc import AbstractRobustChannel, AbstractRobustExchange

EXCHANGE_NAME = "maintainer_lens.investigations"

QUEUE_NAME = "maintainer_lens.investigations.pending"
RETRY_QUEUE_NAME = "maintainer_lens.investigations.retry"
DEAD_QUEUE_NAME = "maintainer_lens.investigations.dead"

ROUTING_KEY = "investigation.requested"
RETRY_ROUTING_KEY = "investigation.retry"
DEAD_ROUTING_KEY = "investigation.dead"

RETRY_DELAY_MS = 60_000


async def declare_investigation_topology(
    channel: AbstractRobustChannel,
) -> AbstractRobustExchange:
    """Declare durable investigation queues, exchange, and bindings."""
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME,
        type=aio_pika.ExchangeType.DIRECT,
        durable=True,
    )

    # Main investigation queue.
    pending_queue = await channel.declare_queue(
        QUEUE_NAME,
        durable=True,
    )
    await pending_queue.bind(
        exchange,
        routing_key=ROUTING_KEY,
    )

    # Delayed retry queue.
    # Messages expire and return to the pending queue via dead-letter routing.
    retry_queue = await channel.declare_queue(
        RETRY_QUEUE_NAME,
        durable=True,
        arguments={
            "x-message-ttl": RETRY_DELAY_MS,
            "x-dead-letter-exchange": EXCHANGE_NAME,
            "x-dead-letter-routing-key": ROUTING_KEY,
        },
    )
    await retry_queue.bind(
        exchange,
        routing_key=RETRY_ROUTING_KEY,
    )

    # Terminal dead-letter queue.
    dead_queue = await channel.declare_queue(
        DEAD_QUEUE_NAME,
        durable=True,
    )
    await dead_queue.bind(exchange, routing_key=DEAD_ROUTING_KEY)

    return exchange
