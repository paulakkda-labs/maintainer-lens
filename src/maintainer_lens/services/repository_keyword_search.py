"""Search repository source chunks by literal code identifiers."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.repository_index import (
    RepositoryChunk,
    RepositoryIndex,
)
from maintainer_lens.services.repository_vector_search import (
    RepositoryIndexNotFound,
    RepositoryIndexNotReady,
)


@dataclass(frozen=True, slots=True)
class KeywordSearchResult:
    """A keyword-matched source chunk with provenance."""

    index_id: UUID
    chunk_key: str
    repository: str
    commit_sha: str
    file_path: str
    blob_sha: str
    language: str
    kind: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str
    matched_in: str


def _make_like_pattern(keyword: str) -> str:
    """Escape SQL LIKE wildcards for literal substring matching."""

    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    return f"%{escaped}%"


async def search_repository_by_keyword(
    session: AsyncSession,
    index_id: UUID,
    keyword: str,
    *,
    top_k: int = 5,
) -> list[KeywordSearchResult]:
    """Search literal identifiers within one ready repository index."""

    normalized_keyword = keyword.strip()

    if not normalized_keyword:
        raise ValueError("Search keyword cannot be empty")

    if len(normalized_keyword) > 128:
        raise ValueError("Search keyword is too long")

    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    async with session.begin():
        index = await session.get(RepositoryIndex, index_id)

        if index is None:
            raise RepositoryIndexNotFound(f"Repository index {index_id} does not exist")

        if index.status != "ready":
            raise RepositoryIndexNotReady(f"Repository index {index_id} is not ready")

        pattern = _make_like_pattern(normalized_keyword)

        symbol_match = RepositoryChunk.symbol.ilike(
            pattern,
            escape="\\",
        )

        path_match = RepositoryChunk.file_path.ilike(
            pattern,
            escape="\\",
        )

        content_match = RepositoryChunk.content.ilike(
            pattern,
            escape="\\",
        )

        match_priority = case(
            (
                func.lower(RepositoryChunk.symbol) == normalized_keyword.lower(),
                0,
            ),
            (symbol_match, 1),
            (path_match, 2),
            else_=3,
        )

        statement = (
            select(
                RepositoryChunk,
                match_priority.label("match_priority"),
            )
            .where(
                RepositoryChunk.index_id == index_id,
                or_(
                    symbol_match,
                    path_match,
                    content_match,
                ),
            )
            .order_by(
                match_priority.asc(),
                RepositoryChunk.file_path.asc(),
                RepositoryChunk.start_line.asc(),
                RepositoryChunk.chunk_key.asc(),
            )
            .limit(top_k)
        )

        rows = (await session.execute(statement)).all()

        results: list[KeywordSearchResult] = []

        for chunk, priority in rows:
            matched_in = (
                "symbol"
                if priority <= 1
                else "file_path"
                if priority == 2
                else "content"
            )

            results.append(
                KeywordSearchResult(
                    index_id=chunk.index_id,
                    chunk_key=chunk.chunk_key,
                    repository=index.repository,
                    commit_sha=index.commit_sha,
                    file_path=chunk.file_path,
                    blob_sha=chunk.blob_sha,
                    language=chunk.language,
                    kind=chunk.kind,
                    symbol=chunk.symbol,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    content=chunk.content,
                    matched_in=matched_in,
                )
            )

        return results
