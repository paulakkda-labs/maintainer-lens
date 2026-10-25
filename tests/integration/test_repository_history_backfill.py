"""Integration tests for resumable repository history backfill."""

import os
from asyncio import Event, TaskGroup
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select

from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.models.repository_history_backfill import (
    RepositoryHistoryBackfillState,
)
from maintainer_lens.db.models.repository_history_sync import (
    RepositoryHistorySyncState,
)
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.services.repository_history_backfill import (
    RepositoryHistoryBackfillConflict,
    backfill_repository_history,
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
                        delete(RepositoryHistoryBackfillState).where(
                            RepositoryHistoryBackfillState.repository == repository
                        )
                    )
                    await session.execute(
                        delete(RepositoryHistorySyncState).where(
                            RepositoryHistorySyncState.repository == repository
                        )
                    )
        finally:
            await engine.dispose()


def _github_item(repository: str, number: int, *, is_pr: bool = False) -> dict:
    path = "pull" if is_pr else "issues"
    item = {
        "number": number,
        "title": f"History item {number}",
        "body": f"Description {number}",
        "state": "closed" if is_pr else "open",
        "html_url": f"https://github.com/{repository}/{path}/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-10-24T00:00:00Z",
    }
    if is_pr:
        item["pull_request"] = {}
    return item


def _page_response(
    repository: str,
    number: int,
    *,
    next_page: int | None = None,
    is_pr: bool = False,
) -> httpx.Response:
    headers = {}
    if next_page is not None:
        headers["Link"] = (
            f"<https://api.github.com/repos/{repository}/issues"
            f'?page={next_page}>; rel="next"'
        )

    return httpx.Response(
        200,
        json=[_github_item(repository, number, is_pr=is_pr)],
        headers=headers,
    )


async def test_backfill_resumes_and_initializes_incremental_checkpoint(
    history_db,
) -> None:
    repository, session_factory = history_db
    requested_pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["sort"] == "created"
        assert request.url.params["direction"] == "asc"
        assert request.url.params["state"] == "all"
        assert request.url.params["per_page"] == "2"
        assert "since" not in request.url.params

        page = int(request.url.params["page"])
        requested_pages.append(page)

        return _page_response(
            repository,
            number=page,
            next_page=page + 1 if page < 3 else None,
            is_pr=page == 2,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await backfill_repository_history(
            session_factory,
            client,
            repository,
            per_page=2,
            max_pages=2,
        )

        assert first.status == "partial"
        assert first.pages_fetched == 2
        assert first.items_fetched == 2
        assert first.next_page == 3
        assert requested_pages == [1, 2]

        async with session_factory() as session:
            state = await session.get(
                RepositoryHistoryBackfillState,
                repository,
            )
            assert state is not None
            assert state.next_page == 3
            assert state.completed_at is None

            second_item = await session.get(
                RepositoryHistoryRecord,
                (repository, 2),
            )
            assert second_item is not None
            assert second_item.kind == "pull_request"

            sync_state = await session.get(
                RepositoryHistorySyncState,
                repository,
            )
            assert sync_state is None

        # The page size is part of the durable cursor identity.
        with pytest.raises(ValueError, match="Cannot change per_page"):
            await backfill_repository_history(
                session_factory,
                client,
                repository,
                per_page=3,
            )

        assert requested_pages == [1, 2]

        second = await backfill_repository_history(
            session_factory,
            client,
            repository,
            per_page=2,
            max_pages=2,
        )

        assert second.status == "completed"
        assert second.next_page is None
        assert second.pages_fetched == 1
        assert requested_pages == [1, 2, 3]

        third = await backfill_repository_history(
            session_factory,
            client,
            repository,
            per_page=2,
        )

        assert third.status == "already_completed"
        assert third.pages_fetched == 0
        assert requested_pages == [1, 2, 3]

    async with session_factory() as session:
        backfill_state = await session.get(
            RepositoryHistoryBackfillState,
            repository,
        )
        sync_state = await session.get(
            RepositoryHistorySyncState,
            repository,
        )
        count = await session.scalar(
            select(func.count())
            .select_from(RepositoryHistoryRecord)
            .where(RepositoryHistoryRecord.repository == repository)
        )

        assert backfill_state is not None
        assert backfill_state.completed_at is not None
        assert sync_state is not None
        assert sync_state.completed_through == backfill_state.started_at
        assert count == 3


async def test_failed_page_does_not_advance_cursor_or_commit_items(
    history_db,
) -> None:
    repository, session_factory = history_db

    def failing_handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 2:
            return httpx.Response(503)
        return _page_response(repository, 1, next_page=2)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(failing_handler)
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await backfill_repository_history(
                session_factory,
                client,
                repository,
                per_page=2,
                max_pages=2,
            )

    async with session_factory() as session:
        state = await session.get(
            RepositoryHistoryBackfillState,
            repository,
        )
        count = await session.scalar(
            select(func.count())
            .select_from(RepositoryHistoryRecord)
            .where(RepositoryHistoryRecord.repository == repository)
        )

        assert state is not None
        assert state.next_page == 1
        assert state.completed_at is None
        assert count == 0

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _page_response(repository, 1))
    ) as client:
        result = await backfill_repository_history(
            session_factory,
            client,
            repository,
            per_page=2,
        )

    assert result.status == "completed"
    assert result.items_fetched == 1


async def test_stale_concurrent_backfill_cannot_commit(
    history_db,
) -> None:
    repository, session_factory = history_db

    stale_request_started = Event()
    release_stale_request = Event()

    async def stale_handler(request: httpx.Request) -> httpx.Response:
        stale_request_started.set()
        await release_stale_request.wait()
        return _page_response(repository, 10)

    def fresh_handler(request: httpx.Request) -> httpx.Response:
        return _page_response(repository, 20)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(stale_handler)) as stale_client,
        httpx.AsyncClient(transport=httpx.MockTransport(fresh_handler)) as fresh_client,
    ):

        async def stale_backfill() -> None:
            with pytest.raises(RepositoryHistoryBackfillConflict):
                await backfill_repository_history(
                    session_factory,
                    stale_client,
                    repository,
                    per_page=2,
                )

        async with TaskGroup() as group:
            group.create_task(stale_backfill())

            await stale_request_started.wait()

            try:
                fresh = await backfill_repository_history(
                    session_factory,
                    fresh_client,
                    repository,
                    per_page=2,
                )
                assert fresh.status == "completed"
            finally:
                release_stale_request.set()

    async with session_factory() as session:
        stale_item = await session.get(
            RepositoryHistoryRecord,
            (repository, 10),
        )
        fresh_item = await session.get(
            RepositoryHistoryRecord,
            (repository, 20),
        )

        assert stale_item is None
        assert fresh_item is not None
