"""Asynchronous RabbitMQ connection configuration."""

import aio_pika
from aio_pika.abc import AbstractRobustConnection


async def create_rabbitmq_connection(
    rabbitmq_url: str,
) -> AbstractRobustConnection:
    """Establish a robust asynchronous RabbitMQ connection."""
    rabbitmq_connection = await aio_pika.connect_robust(rabbitmq_url)
    return rabbitmq_connection
