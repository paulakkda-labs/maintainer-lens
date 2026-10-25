"""PostgreSQL integration tests for history enrichment persistence."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

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

pytestmark = pytest.mark.anyio

BASE_TIME = datetime(2026, 10, 24, tzinfo=UTC)


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
                        delete(RepositoryHistoryCommentRecord).where(
                            RepositoryHistoryCommentRecord.repository == repository
                        )
                    )
                    await session.execute(
                        delete(RepositoryHistoryPullRequestRecord).where(
                            RepositoryHistoryPullRequestRecord.repository == repository
                        )
                    )
                    await session.execute(
                        delete(RepositoryHistoryRecord).where(
                            RepositoryHistoryRecord.repository == repository
                        )
                    )
        finally:
            await engine.dispose()


def _item(
    repository: str,
    number: int,
    *,
    is_pr: bool = False,
    updated_at: datetime = BASE_TIME,
) -> GitHubHistoryItem:
    kind = "pull_request" if is_pr else "issue"
    path = "pull" if is_pr else "issues"

    return GitHubHistoryItem.model_validate(
        {
            "repository": repository,
            "number": number,
            "kind": kind,
            "title": f"History {number}",
            "body": "History body",
            "state": "closed" if is_pr else "open",
            "html_url": (f"https://github.com/{repository}/{path}/{number}"),
            "created_at": BASE_TIME - timedelta(days=10),
            "updated_at": updated_at,
        }
    )


def _comment(
    repository: str,
    *,
    body: str,
    updated_at: datetime,
) -> GitHubHistoryComment:
    return GitHubHistoryComment.model_validate(
        {
            "repository": repository,
            "issue_number": 1,
            "id": 9001,
            "body": body,
            "html_url": (f"https://github.com/{repository}/issues/1#issuecomment-9001"),
            "created_at": BASE_TIME - timedelta(days=1),
            "updated_at": updated_at,
            "user": {"login": "maintainer"},
        }
    )


def _pr(repository: str, *, merged: bool) -> GitHubPullRequestDetails:
    return GitHubPullRequestDetails.model_validate(
        {
            "repository": repository,
            "number": 2,
            "html_url": f"https://github.com/{repository}/pull/2",
            "merged": merged,
            "merged_at": BASE_TIME if merged else None,
            "merge_commit_sha": "c" * 40,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 3,
        }
    )


def _enrichment(
    repository: str,
    issue: GitHubHistoryItem,
    pr_item: GitHubHistoryItem,
    *,
    comment: GitHubHistoryComment,
    details: GitHubPullRequestDetails,
) -> RepositoryHistoryEnrichment:
    return RepositoryHistoryEnrichment(
        repository=repository,
        items=(issue, pr_item),
        comments=(comment,),
        pull_requests=(details,),
        incomplete_comment_numbers=(),
        unprocessed_items=0,
        requests_made=2,
    )


async def _save_parents(
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
        await save_repository_history_batch(session, repository, batch)


async def test_enrichment_is_idempotent_and_preserves_newer_data(
    history_db,
) -> None:
    repository, session_factory = history_db
    issue = _item(repository, 1)
    old_pr_item = _item(repository, 2, is_pr=True)
    new_time = BASE_TIME + timedelta(days=1)
    new_pr_item = _item(
        repository,
        2,
        is_pr=True,
        updated_at=new_time,
    )

    await _save_parents(session_factory, repository, issue, new_pr_item)

    old = _enrichment(
        repository,
        issue,
        old_pr_item,
        comment=_comment(
            repository,
            body="Original comment",
            updated_at=BASE_TIME,
        ),
        details=_pr(repository, merged=False),
    )

    new = _enrichment(
        repository,
        issue,
        new_pr_item,
        comment=_comment(
            repository,
            body="Edited comment",
            updated_at=new_time,
        ),
        details=_pr(repository, merged=True),
    )

    for result in (old, new, old, new):
        async with session_factory() as session:
            await save_repository_history_enrichment(session, result)

    async with session_factory() as session:
        comment = await session.get(
            RepositoryHistoryCommentRecord,
            (repository, 9001),
        )
        pr_details = await session.get(
            RepositoryHistoryPullRequestRecord,
            (repository, 2),
        )

        comment_count = await session.scalar(
            select(func.count())
            .select_from(RepositoryHistoryCommentRecord)
            .where(RepositoryHistoryCommentRecord.repository == repository)
        )
        pr_count = await session.scalar(
            select(func.count())
            .select_from(RepositoryHistoryPullRequestRecord)
            .where(RepositoryHistoryPullRequestRecord.repository == repository)
        )

        assert comment is not None
        assert comment.body == "Edited comment"
        assert comment.source_updated_at == new_time
        assert comment_count == 1

        assert pr_details is not None
        assert pr_details.merged is True
        assert pr_details.source_item_updated_at == new_time
        assert pr_count == 1


async def test_pr_foreign_key_failure_rolls_back_comment(
    history_db,
) -> None:
    repository, session_factory = history_db
    issue = _item(repository, 1)
    missing_pr = _item(repository, 2, is_pr=True)

    # Deliberately persist only the issue, not the PR parent.
    await _save_parents(session_factory, repository, issue)

    result = _enrichment(
        repository,
        issue,
        missing_pr,
        comment=_comment(
            repository,
            body="Should roll back",
            updated_at=BASE_TIME,
        ),
        details=_pr(repository, merged=False),
    )

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await save_repository_history_enrichment(session, result)

    async with session_factory() as session:
        comment = await session.get(
            RepositoryHistoryCommentRecord,
            (repository, 9001),
        )
        pr_details = await session.get(
            RepositoryHistoryPullRequestRecord,
            (repository, 2),
        )

        assert comment is None
        assert pr_details is None


async def test_rejects_cross_repository_comment_before_write(
    history_db,
) -> None:
    repository, session_factory = history_db
    issue = _item(repository, 1)

    await _save_parents(session_factory, repository, issue)

    valid_comment = _comment(
        repository,
        body="Valid",
        updated_at=BASE_TIME,
    )
    foreign_comment = valid_comment.model_copy(
        update={"repository": "other/repository"}
    )

    batch = RepositoryHistoryEnrichment(
        repository=repository,
        items=(issue,),
        comments=(foreign_comment,),
        pull_requests=(),
        incomplete_comment_numbers=(),
        unprocessed_items=0,
        requests_made=1,
    )

    async with session_factory() as session:
        with pytest.raises(ValueError, match="Comment does not belong"):
            await save_repository_history_enrichment(session, batch)
