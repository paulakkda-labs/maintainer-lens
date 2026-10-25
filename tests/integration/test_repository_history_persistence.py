"""Integration tests for durable repository history persistence."""

import os
from asyncio import TaskGroup
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from maintainer_lens.clients.github_history import GitHubHistoryItem
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.services.repository_history_collection import (
    RepositoryHistoryBatch,
)
from maintainer_lens.services.repository_history_persistence import (
    save_repository_history_batch,
)

BASE_TIME = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _item(
    repository: str,
    number: int,
    *,
    kind: str = "issue",
    title: str = "Original title",
    updated_at: datetime = BASE_TIME,
) -> GitHubHistoryItem:
    path = "pull" if kind == "pull_request" else "issues"

    return GitHubHistoryItem.model_validate(
        {
            "repository": repository,
            "number": number,
            "kind": kind,
            "title": title,
            "body": f"Description for {number}",
            "state": "closed" if kind == "pull_request" else "open",
            "html_url": f"https://github.com/{repository}/{path}/{number}",
            "created_at": datetime(2026, 10, 1, tzinfo=UTC),
            "updated_at": updated_at,
        }
    )


def _batch(*items: GitHubHistoryItem) -> RepositoryHistoryBatch:
    return RepositoryHistoryBatch(
        items=items,
        pages_fetched=1,
        stop_reason="exhausted",
    )


@pytest.mark.anyio
async def test_history_batch_upsert_and_stale_update_protection() -> None:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration test is disabled")

    repository = f"ml-test-{uuid4().hex}/history"
    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)

    try:
        issue = _item(repository, 10)
        pull_request = _item(
            repository,
            11,
            kind="pull_request",
        )

        # Insert the first batch.
        async with session_factory() as session:
            await save_repository_history_batch(
                session,
                repository,
                _batch(issue, pull_request),
            )

        async with session_factory() as session:
            saved_issue = await session.get(
                RepositoryHistoryRecord,
                (repository, 10),
            )
            saved_pr = await session.get(
                RepositoryHistoryRecord,
                (repository, 11),
            )

            assert saved_issue is not None
            assert saved_issue.kind == "issue"
            assert saved_issue.title == "Original title"
            assert saved_issue.fetched_at is not None

            assert saved_pr is not None
            assert saved_pr.kind == "pull_request"

        # Apply an updated GitHub version.
        newer_time = BASE_TIME + timedelta(days=1)
        newer_issue = _item(
            repository,
            10,
            title="Updated title",
            updated_at=newer_time,
        )

        async with session_factory() as session:
            await save_repository_history_batch(
                session,
                repository,
                _batch(newer_issue),
            )

        # Replay an older version after the newer version was committed.
        async with session_factory() as session:
            await save_repository_history_batch(
                session,
                repository,
                _batch(issue),
            )

        # Replay the current version to check idempotent storage.
        async with session_factory() as session:
            await save_repository_history_batch(
                session,
                repository,
                _batch(newer_issue),
            )

        async with session_factory() as session:
            saved_issue = await session.get(
                RepositoryHistoryRecord,
                (repository, 10),
            )

            count = await session.scalar(
                select(func.count())
                .select_from(RepositoryHistoryRecord)
                .where(RepositoryHistoryRecord.repository == repository)
            )

            assert saved_issue is not None
            assert saved_issue.title == "Updated title"
            assert saved_issue.source_updated_at == newer_time
            assert count == 2

        # Reject mismatched repository data before writing.
        foreign_item = _item("another/repository", 20)

        async with session_factory() as session:
            with pytest.raises(ValueError, match="another repository"):
                await save_repository_history_batch(
                    session,
                    repository,
                    _batch(foreign_item),
                )

        # Reject duplicate identifiers within a single input batch.
        async with session_factory() as session:
            with pytest.raises(ValueError, match="Duplicate history number"):
                await save_repository_history_batch(
                    session,
                    repository,
                    _batch(issue, issue),
                )

    finally:
        try:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(RepositoryHistoryRecord).where(
                            RepositoryHistoryRecord.repository == repository
                        )
                    )
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_concurrent_history_writes_preserve_newer_version() -> None:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration test is disabled")

    repository = f"ml-test-{uuid4().hex}/history"
    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)

    older = _item(
        repository,
        100,
        title="Older",
        updated_at=BASE_TIME,
    )
    newer = _item(
        repository,
        100,
        title="Newer",
        updated_at=BASE_TIME + timedelta(days=1),
    )

    async def write_item(item: GitHubHistoryItem) -> None:
        async with session_factory() as session:
            await save_repository_history_batch(
                session,
                repository,
                _batch(item),
            )

    try:
        # Each concurrent task owns a separate AsyncSession.
        async with TaskGroup() as group:
            group.create_task(write_item(older))
            group.create_task(write_item(newer))

        async with session_factory() as session:
            saved = await session.get(
                RepositoryHistoryRecord,
                (repository, 100),
            )

            assert saved is not None
            assert saved.title == "Newer"
            assert saved.source_updated_at == newer.updated_at

    finally:
        try:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(RepositoryHistoryRecord).where(
                            RepositoryHistoryRecord.repository == repository
                        )
                    )
        finally:
            await engine.dispose()
