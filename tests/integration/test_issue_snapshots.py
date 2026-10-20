"""Integration tests for investigation issue snapshot persistence."""

import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_tasks import create_investigation_task
from maintainer_lens.db.issue_snapshots import (
    IssueSnapshotData,
    upsert_issue_snapshot,
)
from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationTask,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


@pytest.fixture
async def snapshot_task_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    """Create and clean up an investigation task."""
    task_id = f"snapshot-{uuid4().hex}"

    async with session_factory() as session:
        async with session.begin():
            await create_investigation_task(
                session=session,
                task_id=task_id,
                repository="encode/httpx",
                issue_number=3712,
            )

    try:
        yield task_id
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(InvestigationTask).where(InvestigationTask.id == task_id)
                )


async def set_task_claim(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    token: str,
    lease_seconds: int = 600,
) -> None:
    """Set task ownership for persistence isolation tests."""
    lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(
                    status="running",
                    claim_token=token,
                    lease_expires_at=lease_expires_at,
                )
            )


def make_snapshot() -> IssueSnapshotData:
    """Build issue data for persistence tests."""
    return IssueSnapshotData(
        title="Original issue title",
        body="Original issue description",
        state="open",
        source_url="https://github.com/encode/httpx/issues/3712",
        source_created_at=datetime(2026, 10, 1, tzinfo=UTC),
        source_updated_at=datetime(2026, 10, 2, tzinfo=UTC),
    )


async def save_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    token: str,
    snapshot: IssueSnapshotData,
) -> bool:
    """Save an issue snapshot inside a committed transaction."""
    async with session_factory() as session:
        async with session.begin():
            return await upsert_issue_snapshot(
                session,
                task_id=task_id,
                claim_token=token,
                snapshot=snapshot,
            )


@pytest.mark.anyio
async def test_snapshot_upsert_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot_task_id: str,
) -> None:
    """Repeated writes update the same snapshot row."""
    token = "worker-a-token"

    await set_task_claim(session_factory, snapshot_task_id, token)

    original = make_snapshot()

    assert await save_snapshot(
        session_factory,
        snapshot_task_id,
        token,
        original,
    )

    updated = replace(
        original,
        title="Updated issue title",
        body="Updated issue description",
    )

    assert await save_snapshot(
        session_factory,
        snapshot_task_id,
        token,
        updated,
    )

    async with session_factory() as session:
        snapshot = await session.get(
            InvestigationIssueSnapshot,
            snapshot_task_id,
        )

        count = await session.scalar(
            select(func.count())
            .select_from(InvestigationIssueSnapshot)
            .where(InvestigationIssueSnapshot.task_id == snapshot_task_id)
        )

    assert snapshot is not None
    assert snapshot.title == "Updated issue title"
    assert snapshot.body == "Updated issue description"
    assert count == 1


@pytest.mark.anyio
async def test_old_worker_cannot_overwrite_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot_task_id: str,
) -> None:
    """Reject writes from a worker that no longer owns the task."""
    old_token = "worker-a-token"
    new_token = "worker-b-token"

    await set_task_claim(
        session_factory,
        snapshot_task_id,
        old_token,
    )

    await set_task_claim(
        session_factory,
        snapshot_task_id,
        new_token,
    )

    current_snapshot = replace(
        make_snapshot(),
        title="Worker B result",
    )

    assert await save_snapshot(
        session_factory,
        snapshot_task_id,
        new_token,
        current_snapshot,
    )

    stale_snapshot = replace(
        make_snapshot(),
        title="Worker A stale result",
    )

    assert not await save_snapshot(
        session_factory,
        snapshot_task_id,
        old_token,
        stale_snapshot,
    )

    async with session_factory() as session:
        snapshot = await session.get(
            InvestigationIssueSnapshot,
            snapshot_task_id,
        )

    assert snapshot is not None
    assert snapshot.title == "Worker B result"


@pytest.mark.anyio
async def test_expired_lease_rejects_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot_task_id: str,
) -> None:
    """Reject snapshot writes when the task lease has expired."""
    token = "expired-worker-token"

    await set_task_claim(
        session_factory,
        snapshot_task_id,
        token,
        lease_seconds=-600,
    )

    saved = await save_snapshot(
        session_factory,
        snapshot_task_id,
        token,
        make_snapshot(),
    )

    assert saved is False

    async with session_factory() as session:
        snapshot = await session.get(
            InvestigationIssueSnapshot,
            snapshot_task_id,
        )

    assert snapshot is None


@pytest.mark.anyio
async def test_snapshot_write_rolls_back_with_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    snapshot_task_id: str,
) -> None:
    """Roll back snapshot changes when the transaction fails."""
    token = "rollback-worker-token"

    await set_task_claim(
        session_factory,
        snapshot_task_id,
        token,
    )

    with pytest.raises(RuntimeError, match="Simulated failure"):
        async with session_factory() as session:
            async with session.begin():
                saved = await upsert_issue_snapshot(
                    session,
                    task_id=snapshot_task_id,
                    claim_token=token,
                    snapshot=make_snapshot(),
                )

                assert saved is True

                raise RuntimeError("Simulated failure")

    async with session_factory() as session:
        snapshot = await session.get(
            InvestigationIssueSnapshot,
            snapshot_task_id,
        )

    assert snapshot is None
