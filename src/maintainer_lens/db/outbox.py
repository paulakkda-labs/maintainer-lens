"""Database operations for investigation outbox delivery."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.investigation import InvestigationOutboxEvent


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    """An outbox event claimed for delivery."""

    id: UUID
    task_id: str
    event_type: str
    payload: dict[str, Any]
    delivery_token: str


async def claim_next_outbox_event(
    session: AsyncSession,
) -> ClaimedOutboxEvent | None:
    """Claim one unpublished event with an expired or missing lease."""

    stmt = (
        select(InvestigationOutboxEvent)
        .where(
            InvestigationOutboxEvent.published_at.is_(None),
            or_(
                InvestigationOutboxEvent.delivery_lease_expires_at.is_(None),
                InvestigationOutboxEvent.delivery_lease_expires_at
                <= func.clock_timestamp(),
            ),
        )
        .order_by(
            InvestigationOutboxEvent.created_at,
            InvestigationOutboxEvent.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    event = await session.scalar(stmt)

    if event is None:
        return None

    token = uuid4().hex

    event.delivery_token = token
    event.delivery_lease_expires_at = func.clock_timestamp() + text(
        "INTERVAL '5 minutes'"
    )
    event.publish_attempts += 1

    await session.flush()

    return ClaimedOutboxEvent(
        id=event.id,
        task_id=event.task_id,
        event_type=event.event_type,
        payload=dict(event.payload),
        delivery_token=token,
    )


async def mark_outbox_published(
    session: AsyncSession,
    *,
    event_id: UUID,
    delivery_token: str,
) -> bool:
    """Mark an event published only while its delivery lease is valid."""

    stmt = (
        update(InvestigationOutboxEvent)
        .where(
            InvestigationOutboxEvent.id == event_id,
            InvestigationOutboxEvent.published_at.is_(None),
            InvestigationOutboxEvent.delivery_token == delivery_token,
            InvestigationOutboxEvent.delivery_lease_expires_at > func.clock_timestamp(),
        )
        .values(
            published_at=func.clock_timestamp(),
            delivery_token=None,
            delivery_lease_expires_at=None,
        )
        .returning(InvestigationOutboxEvent.id)
    )

    updated_id = await session.scalar(stmt)
    return updated_id is not None


async def release_outbox_delivery_for_retry(
    session: AsyncSession,
    *,
    event_id: UUID,
    delivery_token: str,
    retry_delay_seconds: int = 60,
) -> bool:
    """Release a failed delivery and defer its next claim."""

    if type(retry_delay_seconds) is not int or retry_delay_seconds <= 0:
        raise ValueError("Retry delay must be a positive integer")

    stmt = (
        update(InvestigationOutboxEvent)
        .where(
            InvestigationOutboxEvent.id == event_id,
            InvestigationOutboxEvent.published_at.is_(None),
            InvestigationOutboxEvent.delivery_token == delivery_token,
            InvestigationOutboxEvent.delivery_lease_expires_at > func.clock_timestamp(),
        )
        .values(
            delivery_token=None,
            delivery_lease_expires_at=(
                func.clock_timestamp() + timedelta(seconds=retry_delay_seconds)
            ),
        )
        .returning(InvestigationOutboxEvent.id)
    )

    updated_id = await session.scalar(stmt)
    return updated_id is not None
