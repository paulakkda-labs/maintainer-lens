"""Integration tests for durable repository history synchronization."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

import maintainer_lens.services.repository_history_sync as sync_module
from maintainer_lens.clients.github_history import GitHubHistoryItem
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.models.repository_history_sync import (
    RepositoryHistorySyncState,
)
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.services.repository_history_collection import (
    RepositoryHistoryBatch,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def history_db():
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration test is disabled")

    repository = f"ml-test-{uuid4().hex}/history"
    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)

    try:
        yield repository, session_factory
    finally:
        try:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(RepositoryHistoryRecord).where(
                            RepositoryHistoryRecord.repository == repository
                        )
                    )
                    await session.execute(
                        delete(RepositoryHistorySyncState).where(
                            RepositoryHistorySyncState.repository == repository
                        )
                    )
        finally:
            await engine.dispose()


def _item(
    repository: str,
    number: int,
    *,
    title: str = "Original title",
    updated_at: datetime | None = None,
) -> GitHubHistoryItem:
    now = datetime.now(UTC)

    return GitHubHistoryItem.model_validate(
        {
            "repository": repository,
            "number": number,
            "kind": "issue",
            "title": title,
            "body": "History content",
            "state": "open",
            "html_url": (f"https://github.com/{repository}/issues/{number}"),
            "created_at": now - timedelta(days=30),
            "updated_at": updated_at or now - timedelta(hours=1),
        }
    )


def _batch(
    *items: GitHubHistoryItem,
    stop_reason: str = "exhausted",
    pages_fetched: int = 1,
) -> RepositoryHistoryBatch:
    return RepositoryHistoryBatch(
        items=items,
        pages_fetched=pages_fetched,
        stop_reason=stop_reason,
    )


async def test_sync_reuses_checkpoint_and_preserves_partial_data(
    history_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, session_factory = history_db

    now = datetime.now(UTC)

    original = _item(
        repository,
        1,
        updated_at=now - timedelta(hours=1),
    )
    partial_item = _item(
        repository,
        2,
        updated_at=now - timedelta(minutes=30),
    )
    updated = _item(
        repository,
        1,
        title="Updated title",
        updated_at=now - timedelta(minutes=10),
    )

    batches = iter(
        [
            _batch(original),
            _batch(partial_item, stop_reason="item_limit"),
            _batch(updated),
        ]
    )

    requested_times = []

    async def fake_collect(
        http_client,
        target_repository,
        **kwargs,
    ):
        assert target_repository == repository
        requested_times.append(kwargs["updated_since"])
        return next(batches)

    monkeypatch.setattr(
        sync_module,
        "collect_repository_history",
        fake_collect,
    )

    async with httpx.AsyncClient() as http_client:
        first = await sync_module.sync_repository_history(
            session_factory,
            http_client,
            repository,
        )

        assert first.requested_since is None
        assert requested_times[0] is None

        async with session_factory() as session:
            state = await session.get(
                RepositoryHistorySyncState,
                repository,
            )
            assert state is not None
            assert state.completed_through == first.started_at
            assert state.last_stop_reason == "exhausted"

        second = await sync_module.sync_repository_history(
            session_factory,
            http_client,
            repository,
            max_items=1,
        )

        assert second.requested_since == (first.started_at - timedelta(days=1))
        assert requested_times[1] == second.requested_since

        async with session_factory() as session:
            state = await session.get(
                RepositoryHistorySyncState,
                repository,
            )
            saved_partial = await session.get(
                RepositoryHistoryRecord,
                (repository, 2),
            )

            assert state is not None
            assert state.completed_through == first.started_at
            assert state.last_stop_reason == "item_limit"
            assert saved_partial is not None

        third = await sync_module.sync_repository_history(
            session_factory,
            http_client,
            repository,
        )

    assert third.requested_since == (first.started_at - timedelta(days=1))

    async with session_factory() as session:
        state = await session.get(
            RepositoryHistorySyncState,
            repository,
        )
        saved = await session.get(
            RepositoryHistoryRecord,
            (repository, 1),
        )
        count = await session.scalar(
            select(func.count())
            .select_from(RepositoryHistoryRecord)
            .where(RepositoryHistoryRecord.repository == repository)
        )

        assert state is not None
        assert state.completed_through == third.started_at
        assert state.last_stop_reason == "exhausted"
        assert state.last_item_count == 1
        assert saved is not None
        assert saved.title == "Updated title"
        assert count == 2


async def test_failed_checkpoint_write_rolls_back_history(
    history_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, session_factory = history_db
    item = _item(repository, 100)

    # An invalid pages_fetched value deliberately violates the DB constraint.
    bad_batch = _batch(
        item,
        pages_fetched=0,
    )

    async def fake_collect(
        http_client,
        target_repository,
        **kwargs,
    ):
        return bad_batch

    monkeypatch.setattr(
        sync_module,
        "collect_repository_history",
        fake_collect,
    )

    async with httpx.AsyncClient() as http_client:
        with pytest.raises(IntegrityError):
            await sync_module.sync_repository_history(
                session_factory,
                http_client,
                repository,
            )

    async with session_factory() as session:
        saved = await session.get(
            RepositoryHistoryRecord,
            (repository, 100),
        )
        state = await session.get(
            RepositoryHistorySyncState,
            repository,
        )

        assert saved is None
        assert state is None


async def test_collection_failure_does_not_change_database(
    history_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, session_factory = history_db

    async def fake_collect(
        http_client,
        target_repository,
        **kwargs,
    ):
        raise httpx.ConnectError("Simulated GitHub failure")

    monkeypatch.setattr(
        sync_module,
        "collect_repository_history",
        fake_collect,
    )

    async with httpx.AsyncClient() as http_client:
        with pytest.raises(httpx.ConnectError):
            await sync_module.sync_repository_history(
                session_factory,
                http_client,
                repository,
            )

    async with session_factory() as session:
        saved = await session.scalar(
            select(func.count())
            .select_from(RepositoryHistoryRecord)
            .where(RepositoryHistoryRecord.repository == repository)
        )
        state = await session.get(
            RepositoryHistorySyncState,
            repository,
        )

        assert saved == 0
        assert state is None
