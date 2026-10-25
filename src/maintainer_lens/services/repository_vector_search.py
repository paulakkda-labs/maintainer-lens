"""Retrieve repository source chunks by vector similarity."""

import math
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.repository_index import (
    RepositoryChunk,
    RepositoryIndex,
)


@dataclass(frozen=True, slots=True)
class RetrievedCodeChunk:
    """A source chunk with its provenance and cosine distance."""

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
    distance: float


class RepositoryIndexNotFound(LookupError):
    """The requested repository index does not exist."""


class RepositoryIndexNotReady(RuntimeError):
    """The requested repository index is not ready for retrieval."""


async def search_repository_chunks(
    session: AsyncSession,
    index_id: UUID,
    query_vector: list[float],
    *,
    top_k: int = 5,
) -> list[RetrievedCodeChunk]:
    """Return the nearest source chunks from one ready index."""

    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    if not query_vector:
        raise ValueError("Query vector cannot be empty")

    if not all(math.isfinite(value) for value in query_vector):
        raise ValueError("Query vector contains non-finite values")

    if not any(value != 0 for value in query_vector):
        raise ValueError("Query vector cannot be zero")

    async with session.begin():
        index = await session.scalar(
            select(RepositoryIndex).where(RepositoryIndex.id == index_id)
        )

        if index is None:
            raise RepositoryIndexNotFound(f"Repository index {index_id} does not exist")

        if index.status != "ready":
            raise RepositoryIndexNotReady(f"Repository index {index_id} is not ready")

        if len(query_vector) != index.embedding_dimensions:
            raise ValueError(
                "Query embedding dimension mismatch: "
                f"expected {index.embedding_dimensions}, "
                f"received {len(query_vector)}"
            )

        cosine_distance = RepositoryChunk.embedding.cosine_distance(query_vector)

        statement = (
            select(
                RepositoryChunk,
                cosine_distance.label("distance"),
            )
            .where(RepositoryChunk.index_id == index_id)
            .order_by(
                cosine_distance.asc(),
                RepositoryChunk.chunk_key.asc(),
            )
            .limit(top_k)
        )

        rows = (await session.execute(statement)).all()

        return [
            RetrievedCodeChunk(
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
                distance=float(distance),
            )
            for chunk, distance in rows
        ]
