"""Publish claimed investigation outbox events to RabbitMQ."""

import asyncio
import logging

from aio_pika.abc import AbstractRobustExchange
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.outbox import (
    claim_next_outbox_event,
    mark_outbox_published,
    release_outbox_delivery_for_retry,
)
from maintainer_lens.messaging.publisher import publish_investigation
from maintainer_lens.messaging.schemas import (
    INVESTIGATION_REQUESTED_EVENT_TYPE,
    InvestigationRequestedMessage,
)

logger = logging.getLogger(__name__)

PUBLISH_TIMEOUT_SECONDS = 30
PUBLISH_RETRY_DELAY_SECONDS = 60


class OutboxDeliveryOwnershipLostError(Exception):
    """The dispatcher no longer owns the claimed outbox event."""


async def dispatch_one_outbox_event(
    session_factory: async_sessionmaker[AsyncSession],
    exchange: AbstractRobustExchange,
) -> bool:
    """Publish one pending outbox event with delivery ownership checks."""

    # Transaction 1: Claim an unpublished event.
    async with session_factory.begin() as session:
        claimed = await claim_next_outbox_event(session)

    if claimed is None:
        return False

    try:
        if claimed.event_type != INVESTIGATION_REQUESTED_EVENT_TYPE:
            raise ValueError(f"Unsupported outbox event type: {claimed.event_type}")

        message = InvestigationRequestedMessage.model_validate(claimed.payload)

        if message.task_id != claimed.task_id:
            raise ValueError("Outbox payload task ID does not match event")
        if message.generation != claimed.generation:
            raise ValueError("Outbox payload generation does not match event")

        # No database transaction is held while publishing.
        async with asyncio.timeout(PUBLISH_TIMEOUT_SECONDS):
            await publish_investigation(
                exchange=exchange,
                task_id=message.task_id,
                repository=message.repository,
                issue_number=message.issue_number,
                source_index_id=message.source_index_id,
                auto_prepare_knowledge=message.auto_prepare_knowledge,
                **({"generation": message.generation} if message.generation else {}),
                **(
                    {"response_language": message.response_language}
                    if message.response_language != "zh"
                    else {}
                ),
            )

    except Exception:
        # Publishing failed. Preserve the original exception.
        try:
            async with session_factory.begin() as session:
                released = await release_outbox_delivery_for_retry(
                    session,
                    event_id=claimed.id,
                    delivery_token=claimed.delivery_token,
                    retry_delay_seconds=PUBLISH_RETRY_DELAY_SECONDS,
                )

            if not released:
                logger.warning(
                    "Outbox delivery ownership lost during release: %s",
                    claimed.id,
                )

        except Exception:
            logger.exception(
                "Failed to schedule outbox delivery retry: %s",
                claimed.id,
            )

        raise

    # Transaction 2: Confirm publication in PostgreSQL.
    async with session_factory.begin() as session:
        published = await mark_outbox_published(
            session,
            event_id=claimed.id,
            delivery_token=claimed.delivery_token,
        )

    if not published:
        raise OutboxDeliveryOwnershipLostError(
            f"Outbox delivery ownership lost: {claimed.id}"
        )

    logger.info(
        "Outbox event published successfully: event_id=%s task_id=%s",
        claimed.id,
        claimed.task_id,
    )

    return True
