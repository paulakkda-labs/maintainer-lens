"""Integration tests for repository history keyword search."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete

from maintainer_lens.clients.github_history import GitHubHistoryItem
from maintainer_lens.clients.github_history_details import (
    GitHubHistoryComment,
    GitHubPullRequestDetails,
)
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.models.repository_history_details import (
    RepositoryHistoryCommentRecord,
    RepositoryHistoryPullRequestRecord,
)
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.services.repository_history_collection import (
    RepositoryHistoryBatch,
)
from maintainer_lens.services.repository_history_enrichment import (
    RepositoryHistoryEnrichment,
    save_repository_history_enrichment,
)
from maintainer_lens.services.repository_history_persistence import (
    save_repository_history_batch,
)
from maintainer_lens.services.repository_history_search import (
    search_repository_history,
)

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 25, tzinfo=UTC)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def history_db():
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration test is disabled")

    repository = f"ml-test-{uuid4().hex}/history"
    other_repository = f"ml-test-{uuid4().hex}/history"

    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)

    repositories = (repository, other_repository)

    try:
        yield repository, other_repository, session_factory
    finally:
        try:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(RepositoryHistoryCommentRecord).where(
                            RepositoryHistoryCommentRecord.repository.in_(repositories)
                        )
                    )
                    await session.execute(
                        delete(RepositoryHistoryPullRequestRecord).where(
                            RepositoryHistoryPullRequestRecord.repository.in_(
                                repositories
                            )
                        )
                    )
                    await session.execute(
                        delete(RepositoryHistoryRecord).where(
                            RepositoryHistoryRecord.repository.in_(repositories)
                        )
                    )
        finally:
            await engine.dispose()


def _item(
    repository: str,
    number: int,
    title: str,
    body: str,
    *,
    is_pr: bool = False,
) -> GitHubHistoryItem:
    kind = "pull_request" if is_pr else "issue"
    path = "pull" if is_pr else "issues"

    return GitHubHistoryItem.model_validate(
        {
            "repository": repository,
            "number": number,
            "kind": kind,
            "title": title,
            "body": body,
            "state": "closed" if is_pr else "open",
            "html_url": f"https://github.com/{repository}/{path}/{number}",
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


async def _save_items(
    session_factory,
    repository: str,
    *items: GitHubHistoryItem,
) -> None:
    batch = RepositoryHistoryBatch(
        items=items,
        pages_fetched=1,
        stop_reason="exhausted",
    )

    async with session_factory() as session:
        await save_repository_history_batch(
            session,
            repository,
            batch,
        )


async def test_search_returns_items_comments_and_pr_merge_status(
    history_db,
) -> None:
    repository, _, session_factory = history_db

    issue = _item(
        repository,
        1,
        "AsyncClient cancellation failure",
        "The socket cleanup path is incomplete.",
    )
    pr_item = _item(
        repository,
        2,
        "Fix AsyncClient cancellation",
        "Handle cancellation and release connections.",
        is_pr=True,
    )

    await _save_items(session_factory, repository, issue, pr_item)

    comment = GitHubHistoryComment.model_validate(
        {
            "repository": repository,
            "issue_number": 1,
            "id": 9001,
            "body": "Cancellation recovery discussion.",
            "html_url": (f"https://github.com/{repository}/issues/1#issuecomment-9001"),
            "created_at": NOW,
            "updated_at": NOW,
            "user": {"login": "maintainer"},
        }
    )

    details = GitHubPullRequestDetails.model_validate(
        {
            "repository": repository,
            "number": 2,
            "html_url": f"https://github.com/{repository}/pull/2",
            "merged": True,
            "merged_at": NOW,
            "merge_commit_sha": "c" * 40,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 2,
        }
    )

    enrichment = RepositoryHistoryEnrichment(
        repository=repository,
        items=(issue, pr_item),
        comments=(comment,),
        pull_requests=(details,),
        incomplete_comment_numbers=(),
        unprocessed_items=0,
        requests_made=2,
    )

    async with session_factory() as session:
        await save_repository_history_enrichment(session, enrichment)

    async with session_factory() as session:
        hits = await search_repository_history(
            session,
            repository,
            "cancellation",
        )

        assert len(hits) == 3
        assert hits[0].kind == "pull_request"

        issue_hit = next(
            hit for hit in hits if hit.record_type == "item" and hit.number == 1
        )
        pr_hit = next(
            hit for hit in hits if hit.record_type == "item" and hit.number == 2
        )
        comment_hit = next(hit for hit in hits if hit.record_type == "comment")

        assert issue_hit.kind == "issue"
        assert issue_hit.merged is None

        assert pr_hit.merged is True
        assert pr_hit.source_url.endswith("/pull/2")

        assert comment_hit.number == 1
        assert comment_hit.comment_id == 9001
        assert comment_hit.source_url.endswith("#issuecomment-9001")

        comment_matches = await search_repository_history(
            session,
            repository,
            "discussion",
        )

        assert len(comment_matches) == 1
        assert comment_matches[0].record_type == "comment"

        top_one = await search_repository_history(
            session,
            repository,
            "cancellation",
            top_k=1,
        )

        assert len(top_one) == 1
        assert top_one[0].number == 2


async def test_search_escapes_wildcards_and_isolates_repositories(
    history_db,
) -> None:
    repository, other_repository, session_factory = history_db

    await _save_items(
        session_factory,
        repository,
        _item(
            repository,
            10,
            "metric_100%",
            "Exact identifier.",
        ),
        _item(
            repository,
            11,
            "metricX100Q",
            "Different identifier.",
        ),
    )

    await _save_items(
        session_factory,
        other_repository,
        _item(
            other_repository,
            12,
            "metric_100%",
            "Same keyword in another repository.",
        ),
    )

    async with session_factory() as session:
        hits = await search_repository_history(
            session,
            repository,
            "metric_100%",
        )

        assert len(hits) == 1
        assert hits[0].number == 10
        assert hits[0].repository == repository

        empty = await search_repository_history(
            session,
            repository,
            "definitely_not_present",
        )

        assert empty == ()


async def test_search_rejects_invalid_query_and_limit(
    history_db,
) -> None:
    repository, _, session_factory = history_db

    async with session_factory() as session:
        with pytest.raises(ValueError):
            await search_repository_history(session, repository, " ")

        with pytest.raises(ValueError):
            await search_repository_history(
                session,
                repository,
                "connection",
                top_k=0,
            )

        with pytest.raises(ValueError):
            await search_repository_history(
                session,
                repository,
                "connection",
                top_k=True,
            )

        # Natural-language queries with more than eight terms are allowed.
        await search_repository_history(
            session,
            repository,
            "Why does connecting time out when the origin server responds fast?",
        )

        # More than sixteen unique terms are still rejected.
        with pytest.raises(ValueError, match="at most 16"):
            await search_repository_history(
                session,
                repository,
                (
                    "one two three four five six seven eight "
                    "nine ten eleven twelve thirteen fourteen "
                    "fifteen sixteen seventeen"
                ),
            )
