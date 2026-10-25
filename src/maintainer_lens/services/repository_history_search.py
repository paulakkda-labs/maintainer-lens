"""Search stored GitHub issue, pull-request, and comment history."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import BigInteger, and_, case, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.models.repository_history_details import (
    RepositoryHistoryCommentRecord,
    RepositoryHistoryPullRequestRecord,
)


@dataclass(frozen=True, slots=True)
class RepositoryHistorySearchHit:
    """A historical GitHub record with its original evidence location."""

    repository: str
    number: int
    kind: Literal["issue", "pull_request"]
    record_type: Literal["item", "comment"]
    comment_id: int | None
    title: str
    content: str | None
    source_url: str
    updated_at: datetime
    merged: bool | None
    score: int


def _like_pattern(token: str) -> str:
    """Escape SQL LIKE wildcards and match the token literally."""

    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def search_repository_history(
    session: AsyncSession,
    repository: str,
    query: str,
    *,
    top_k: int = 10,
) -> tuple[RepositoryHistorySearchHit, ...]:
    """Return ranked keyword matches from stored GitHub history."""

    if not repository.strip():
        raise ValueError("Repository must not be blank")

    normalized_query = query.strip()

    if not normalized_query or len(normalized_query) > 256:
        raise ValueError("Query must contain between 1 and 256 characters")

    if type(top_k) is not int or not 1 <= top_k <= 50:
        raise ValueError("top_k must be between 1 and 50")

    tokens = tuple(dict.fromkeys(normalized_query.split()))

    if len(tokens) > 16:
        raise ValueError("Query must contain at most 16 unique terms")

    history = RepositoryHistoryRecord
    comment = RepositoryHistoryCommentRecord
    pull_request = RepositoryHistoryPullRequestRecord

    item_score = literal(0)
    comment_score = literal(0)

    for token in tokens:
        pattern = _like_pattern(token)

        item_score = (
            item_score
            + case(
                (history.title.ilike(pattern, escape="\\"), 3),
                else_=0,
            )
            + case(
                (history.body.ilike(pattern, escape="\\"), 1),
                else_=0,
            )
        )

        comment_score = comment_score + case(
            (comment.body.ilike(pattern, escape="\\"), 1),
            else_=0,
        )

    pr_join = and_(
        pull_request.repository == history.repository,
        pull_request.number == history.number,
    )

    item_query = (
        select(
            literal("item").label("record_type"),
            history.repository.label("repository"),
            history.number.label("number"),
            history.kind.label("kind"),
            literal(None, type_=BigInteger()).label("comment_id"),
            history.title.label("title"),
            history.body.label("content"),
            history.source_url.label("source_url"),
            history.source_updated_at.label("updated_at"),
            pull_request.merged.label("merged"),
            item_score.label("score"),
        )
        .select_from(history)
        .outerjoin(pull_request, pr_join)
        .where(
            history.repository == repository,
            item_score > 0,
        )
    )

    comment_query = (
        select(
            literal("comment").label("record_type"),
            history.repository.label("repository"),
            history.number.label("number"),
            history.kind.label("kind"),
            comment.comment_id.label("comment_id"),
            history.title.label("title"),
            comment.body.label("content"),
            comment.source_url.label("source_url"),
            comment.source_updated_at.label("updated_at"),
            pull_request.merged.label("merged"),
            comment_score.label("score"),
        )
        .select_from(comment)
        .join(
            history,
            and_(
                comment.repository == history.repository,
                comment.issue_number == history.number,
            ),
        )
        .outerjoin(pull_request, pr_join)
        .where(
            history.repository == repository,
            comment_score > 0,
        )
    )

    hits = union_all(item_query, comment_query).subquery("history_hits")

    statement = (
        select(hits)
        .order_by(
            hits.c.score.desc(),
            hits.c.updated_at.desc(),
            hits.c.number.desc(),
            hits.c.comment_id.desc().nulls_last(),
        )
        .limit(top_k)
    )

    result = await session.execute(statement)

    return tuple(
        RepositoryHistorySearchHit(
            repository=row["repository"],
            number=row["number"],
            kind=row["kind"],
            record_type=row["record_type"],
            comment_id=row["comment_id"],
            title=row["title"],
            content=row["content"],
            source_url=row["source_url"],
            updated_at=row["updated_at"],
            merged=row["merged"],
            score=row["score"],
        )
        for row in result.mappings()
    )
