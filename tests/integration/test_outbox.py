"""Integration tests for outbox claiming, leases, and delivery states."""

import os

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import maintainer_lens.services.investigation_submission as submission_module
from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.outbox import (
    claim_next_outbox_event,
    mark_outbox_published,
    release_outbox_delivery_for_retry,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


@pytest.mark.anyio
async def test_outbox_claim_exclusivity_and_lease_recovery(
    investigation_task: tuple[
        async_sessionmaker[AsyncSession],
        str,
        AsyncEngine,
    ],
) -> None:
    """Prevent duplicate claims and recover an expired delivery lease."""

    session_factory, _, _ = investigation_task

    message = await submission_module.create_investigation_submission(
        session_factory,
        repository="encode/httpx",
        issue_number=3712,
    )

    try:
        # Dispatcher A claims the event and holds its row lock.
        async with session_factory.begin() as session_a:
            claimed_a = await claim_next_outbox_event(session_a)

            assert claimed_a is not None
            assert claimed_a.task_id == message.task_id
            assert claimed_a.payload == message.model_dump(mode="json")

            # Dispatcher B must skip the locked event.
            async with session_factory.begin() as session_b:
                claimed_b = await claim_next_outbox_event(session_b)

                assert claimed_b is None

        # A has committed, but its delivery lease is still valid.
        async with session_factory.begin() as session:
            claimed_again = await claim_next_outbox_event(session)

            assert claimed_again is None

        # Simulate a crashed dispatcher whose lease has expired.
        async with session_factory.begin() as session:
            await session.execute(
                update(InvestigationOutboxEvent)
                .where(InvestigationOutboxEvent.task_id == message.task_id)
                .values(
                    delivery_lease_expires_at=(
                        func.clock_timestamp() - text("INTERVAL '1 second'")
                    )
                )
            )

        # A different dispatcher reclaims the event.
        async with session_factory.begin() as session:
            claimed_b = await claim_next_outbox_event(session)

            assert claimed_b is not None
            assert claimed_b.id == claimed_a.id
            assert claimed_b.delivery_token != claimed_a.delivery_token

        async with session_factory() as session:
            event = await session.scalar(
                select(InvestigationOutboxEvent).where(
                    InvestigationOutboxEvent.task_id == message.task_id
                )
            )

            assert event is not None
            assert event.delivery_token == claimed_b.delivery_token
            assert event.publish_attempts == 2
            assert event.published_at is None

    finally:
        async with session_factory.begin() as session:
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == message.task_id)
            )


@pytest.mark.anyio
async def test_outbox_publish_confirmation(
    investigation_task: tuple[
        async_sessionmaker[AsyncSession],
        str,
        AsyncEngine,
    ],
) -> None:
    """Confirm publication and prevent repeated delivery claims."""

    session_factory, _, _ = investigation_task

    message = await submission_module.create_investigation_submission(
        session_factory,
        repository="encode/httpx",
        issue_number=3712,
    )

    try:
        async with session_factory.begin() as session:
            claimed = await claim_next_outbox_event(session)

        assert claimed is not None

        async with session_factory.begin() as session:
            assert await mark_outbox_published(
                session,
                event_id=claimed.id,
                delivery_token=claimed.delivery_token,
            )

        async with session_factory() as session:
            event = await session.get(
                InvestigationOutboxEvent,
                claimed.id,
            )

            assert event is not None
            assert event.published_at is not None
            assert event.delivery_token is None
            assert event.delivery_lease_expires_at is None

        async with session_factory.begin() as session:
            assert not await mark_outbox_published(
                session,
                event_id=claimed.id,
                delivery_token=claimed.delivery_token,
            )

            assert await claim_next_outbox_event(session) is None

    finally:
        async with session_factory.begin() as session:
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == message.task_id)
            )


@pytest.mark.anyio
async def test_outbox_failed_delivery_and_stale_token(
    investigation_task: tuple[
        async_sessionmaker[AsyncSession],
        str,
        AsyncEngine,
    ],
) -> None:
    """Retry failed delivery and reject the former owner's token."""

    session_factory, _, _ = investigation_task

    message = await submission_module.create_investigation_submission(
        session_factory,
        repository="encode/httpx",
        issue_number=3712,
    )

    try:
        async with session_factory.begin() as session:
            claimed_a = await claim_next_outbox_event(session)

        assert claimed_a is not None

        # Dispatcher A fails to publish.
        async with session_factory.begin() as session:
            assert await release_outbox_delivery_for_retry(
                session,
                event_id=claimed_a.id,
                delivery_token=claimed_a.delivery_token,
                retry_delay_seconds=60,
            )

        # The event is unavailable during the retry delay.
        async with session_factory.begin() as session:
            assert await claim_next_outbox_event(session) is None

        # Simulate the retry deadline having passed.
        async with session_factory.begin() as session:
            await session.execute(
                update(InvestigationOutboxEvent)
                .where(InvestigationOutboxEvent.id == claimed_a.id)
                .values(
                    delivery_lease_expires_at=(
                        func.clock_timestamp() - text("INTERVAL '1 second'")
                    )
                )
            )

        # Dispatcher B reclaims the same event.
        async with session_factory.begin() as session:
            claimed_b = await claim_next_outbox_event(session)

        assert claimed_b is not None
        assert claimed_b.id == claimed_a.id
        assert claimed_b.delivery_token != claimed_a.delivery_token

        # Dispatcher A has lost ownership.
        async with session_factory.begin() as session:
            assert not await mark_outbox_published(
                session,
                event_id=claimed_a.id,
                delivery_token=claimed_a.delivery_token,
            )

            assert not await release_outbox_delivery_for_retry(
                session,
                event_id=claimed_a.id,
                delivery_token=claimed_a.delivery_token,
            )

        # Dispatcher B can confirm successful publication.
        async with session_factory.begin() as session:
            assert await mark_outbox_published(
                session,
                event_id=claimed_b.id,
                delivery_token=claimed_b.delivery_token,
            )

        async with session_factory() as session:
            event = await session.get(
                InvestigationOutboxEvent,
                claimed_b.id,
            )

            assert event is not None
            assert event.published_at is not None
            assert event.delivery_token is None

    finally:
        async with session_factory.begin() as session:
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == message.task_id)
            )
