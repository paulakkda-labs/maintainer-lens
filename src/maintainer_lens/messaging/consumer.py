"""RabbitMQ consumers for investigation tasks."""

from collections.abc import Awaitable, Callable

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustQueue


async def consume_investigations(
    queue: AbstractRobustQueue,
    handler: Callable[[AbstractIncomingMessage], Awaitable[None]],
) -> None:
    """Consume investigation messages and acknowledge successful processing."""
    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            await handler(message)
            await message.ack()
