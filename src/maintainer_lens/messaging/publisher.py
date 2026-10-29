"""RabbitMQ message construction for investigation tasks."""

from uuid import UUID

import aio_pika
from aio_pika.abc import AbstractIncomingMessage, AbstractRobustExchange

from maintainer_lens.messaging.schemas import (
    MISSING_TASK_CHECKS_HEADER,
    RETRY_ATTEMPTS_HEADER,
    RETRY_NOT_BEFORE_HEADER,
    InvestigationRequestedMessage,
)
from maintainer_lens.messaging.topology import ROUTING_KEY


def create_investigation_message(
    task_id: str,
    repository: str,
    issue_number: int,
    source_index_id: UUID | None = None,
    auto_prepare_knowledge: bool = False,
    generation: int = 0,
) -> aio_pika.Message:
    """Create a persistent JSON message for an investigation task."""
    payload = InvestigationRequestedMessage(
        task_id=task_id,
        repository=repository,
        issue_number=issue_number,
        source_index_id=source_index_id,
        auto_prepare_knowledge=auto_prepare_knowledge,
        generation=generation,
    )
    message_body = payload.model_dump_json().encode("utf-8")
    message = aio_pika.Message(
        body=message_body,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type="application/json",
        message_id=task_id,
    )

    return message


async def publish_investigation(
    exchange: AbstractRobustExchange,
    task_id: str,
    repository: str,
    issue_number: int,
    source_index_id: UUID | None = None,
    auto_prepare_knowledge: bool = False,
    generation: int = 0,
) -> None:
    """Publish an investigation request to RabbitMQ."""
    message = create_investigation_message(
        task_id=task_id,
        repository=repository,
        issue_number=issue_number,
        source_index_id=source_index_id,
        auto_prepare_knowledge=auto_prepare_knowledge,
        generation=generation,
    )

    await exchange.publish(
        message=message,
        routing_key=ROUTING_KEY,
        mandatory=True,
    )


async def republish_investigation_message(
    exchange: AbstractRobustExchange,
    incoming_message: AbstractIncomingMessage,
    routing_key: str,
    retry_attempts: int,
    missing_task_checks: int | None = None,
    retry_not_before: str | None = None,
) -> None:
    """Republish an investigation message to another routing destination."""
    headers = {
        RETRY_ATTEMPTS_HEADER: retry_attempts,
    }

    if missing_task_checks is not None:
        headers[MISSING_TASK_CHECKS_HEADER] = missing_task_checks

    if retry_not_before is not None:
        headers[RETRY_NOT_BEFORE_HEADER] = retry_not_before

    new_message = aio_pika.Message(
        body=incoming_message.body,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type=incoming_message.content_type,
        message_id=incoming_message.message_id,
        headers=headers,
    )

    await exchange.publish(
        message=new_message,
        routing_key=routing_key,
        mandatory=True,
    )
