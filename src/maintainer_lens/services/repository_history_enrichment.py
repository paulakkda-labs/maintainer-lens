"""Collect and persist bounded GitHub history enrichment."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.clients.github_history import GitHubHistoryItem
from maintainer_lens.clients.github_history_details import (
    GitHubHistoryComment,
    GitHubPullRequestDetails,
    fetch_issue_comments_page,
    fetch_pull_request_details,
)
from maintainer_lens.db.models.repository_history_details import (
    RepositoryHistoryCommentRecord,
    RepositoryHistoryPullRequestRecord,
)


@dataclass(frozen=True, slots=True)
class RepositoryHistoryEnrichment:
    """A bounded collection of comments and pull-request details."""

    repository: str
    items: tuple[GitHubHistoryItem, ...]
    comments: tuple[GitHubHistoryComment, ...]
    pull_requests: tuple[GitHubPullRequestDetails, ...]
    incomplete_comment_numbers: tuple[int, ...]
    unprocessed_items: int
    requests_made: int


async def collect_repository_history_enrichment(
    http_client: httpx.AsyncClient,
    repository: str,
    items: Sequence[GitHubHistoryItem],
    *,
    max_items: int = 5,
    max_comment_pages: int = 2,
    comments_per_page: int = 100,
) -> RepositoryHistoryEnrichment:
    """Fetch bounded details without claiming complete repository coverage."""

    if not repository.strip():
        raise ValueError("Repository must not be blank")
    if type(max_items) is not int or not 1 <= max_items <= 5:
        raise ValueError("max_items must be between 1 and 5")
    if type(max_comment_pages) is not int or not 1 <= max_comment_pages <= 3:
        raise ValueError("max_comment_pages must be between 1 and 3")
    if type(comments_per_page) is not int or not 1 <= comments_per_page <= 100:
        raise ValueError("comments_per_page must be between 1 and 100")

    all_items = tuple(items)
    seen_numbers: set[int] = set()

    for item in all_items:
        if item.repository != repository:
            raise ValueError("History item belongs to another repository")
        if item.number in seen_numbers:
            raise ValueError("Duplicate issue number in enrichment input")
        seen_numbers.add(item.number)

    selected = all_items[:max_items]
    comments: list[GitHubHistoryComment] = []
    pull_requests: list[GitHubPullRequestDetails] = []
    incomplete_comment_numbers: list[int] = []
    requests_made = 0

    for item in selected:
        current_page = 1

        for _ in range(max_comment_pages):
            page = await fetch_issue_comments_page(
                http_client,
                repository,
                item.number,
                page=current_page,
                per_page=comments_per_page,
            )
            # Each content request first verifies public repository metadata.
            requests_made += 2
            comments.extend(page.comments)

            if page.next_page is None:
                break

            if page.next_page != current_page + 1:
                raise ValueError("Unexpected comment pagination sequence")

            current_page = page.next_page
        else:
            incomplete_comment_numbers.append(item.number)

        if item.kind == "pull_request":
            details = await fetch_pull_request_details(
                http_client,
                repository,
                item.number,
            )
            requests_made += 2
            pull_requests.append(details)

    return RepositoryHistoryEnrichment(
        repository=repository,
        items=selected,
        comments=tuple(comments),
        pull_requests=tuple(pull_requests),
        incomplete_comment_numbers=tuple(incomplete_comment_numbers),
        unprocessed_items=len(all_items) - len(selected),
        requests_made=requests_made,
    )


def _require_aware(value: datetime) -> None:
    """Reject timestamps without timezone information."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("GitHub timestamps must be timezone-aware")


async def save_repository_history_enrichment(
    session: AsyncSession,
    enrichment: RepositoryHistoryEnrichment,
) -> None:
    """Atomically upsert comments and pull-request details."""

    repository = enrichment.repository

    if not repository.strip():
        raise ValueError("Repository must not be blank")

    items_by_number: dict[int, GitHubHistoryItem] = {}

    for item in enrichment.items:
        if item.repository != repository:
            raise ValueError("History item belongs to another repository")
        if item.number in items_by_number:
            raise ValueError("Duplicate issue number in enrichment batch")

        _require_aware(item.updated_at)
        items_by_number[item.number] = item

    comment_rows: list[dict] = []
    seen_comment_ids: set[int] = set()

    for comment in enrichment.comments:
        if (
            comment.repository != repository
            or comment.issue_number not in items_by_number
        ):
            raise ValueError("Comment does not belong to enrichment batch")
        if comment.id in seen_comment_ids:
            raise ValueError("Duplicate comment ID in enrichment batch")

        _require_aware(comment.created_at)
        _require_aware(comment.updated_at)
        seen_comment_ids.add(comment.id)

        comment_rows.append(
            {
                "repository": repository,
                "comment_id": comment.id,
                "issue_number": comment.issue_number,
                "author_login": (
                    comment.user.login if comment.user is not None else None
                ),
                "body": comment.body,
                "source_url": str(comment.html_url),
                "source_created_at": comment.created_at,
                "source_updated_at": comment.updated_at,
            }
        )

    pull_request_rows: list[dict] = []
    seen_pull_numbers: set[int] = set()

    for details in enrichment.pull_requests:
        item = items_by_number.get(details.number)

        if (
            details.repository != repository
            or item is None
            or item.kind != "pull_request"
        ):
            raise ValueError("Invalid pull-request enrichment")

        if details.number in seen_pull_numbers:
            raise ValueError("Duplicate pull request in enrichment batch")

        if details.merged_at is not None:
            _require_aware(details.merged_at)

        seen_pull_numbers.add(details.number)

        pull_request_rows.append(
            {
                "repository": repository,
                "number": details.number,
                "merged": details.merged,
                "merged_at": details.merged_at,
                "merge_commit_sha": details.merge_commit_sha,
                "head_sha": details.head.sha,
                "base_sha": details.base.sha,
                "changed_files": details.changed_files,
                "source_item_updated_at": item.updated_at,
            }
        )

    async with session.begin():
        if comment_rows:
            comment_insert = insert(RepositoryHistoryCommentRecord).values(comment_rows)

            comment_upsert = comment_insert.on_conflict_do_update(
                index_elements=[
                    RepositoryHistoryCommentRecord.repository,
                    RepositoryHistoryCommentRecord.comment_id,
                ],
                set_={
                    "issue_number": comment_insert.excluded.issue_number,
                    "author_login": comment_insert.excluded.author_login,
                    "body": comment_insert.excluded.body,
                    "source_url": comment_insert.excluded.source_url,
                    "source_created_at": (comment_insert.excluded.source_created_at),
                    "source_updated_at": (comment_insert.excluded.source_updated_at),
                    "fetched_at": func.clock_timestamp(),
                },
                where=(
                    comment_insert.excluded.source_updated_at
                    >= RepositoryHistoryCommentRecord.source_updated_at
                ),
            )

            await session.execute(comment_upsert)

        if pull_request_rows:
            pr_insert = insert(RepositoryHistoryPullRequestRecord).values(
                pull_request_rows
            )

            pr_upsert = pr_insert.on_conflict_do_update(
                index_elements=[
                    RepositoryHistoryPullRequestRecord.repository,
                    RepositoryHistoryPullRequestRecord.number,
                ],
                set_={
                    "merged": pr_insert.excluded.merged,
                    "merged_at": pr_insert.excluded.merged_at,
                    "merge_commit_sha": pr_insert.excluded.merge_commit_sha,
                    "head_sha": pr_insert.excluded.head_sha,
                    "base_sha": pr_insert.excluded.base_sha,
                    "changed_files": pr_insert.excluded.changed_files,
                    "source_item_updated_at": (
                        pr_insert.excluded.source_item_updated_at
                    ),
                    "fetched_at": func.clock_timestamp(),
                },
                where=(
                    pr_insert.excluded.source_item_updated_at
                    >= RepositoryHistoryPullRequestRecord.source_item_updated_at
                ),
            )

            await session.execute(pr_upsert)
