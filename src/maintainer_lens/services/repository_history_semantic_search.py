"""Retrieve historical GitHub evidence using semantic similarity."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.db.models.repository_history_embedding import (
    RepositoryHistoryEmbedding,
)
from maintainer_lens.services.repository_history_indexing import (
    _validate_vector,
)


@dataclass(frozen=True, slots=True)
class RepositoryHistorySemanticHit:
    """Historical evidence and its cosine distance from a query."""

    repository: str
    number: int
    source_type: Literal["item", "comment"]
    comment_id: int | None
    title: str
    content: str
    source_url: str
    source_updated_at: datetime
    distance: float


async def search_repository_history_semantic(
    session: AsyncSession,
    embedding_client: EmbeddingProvider,
    repository: str,
    query: str,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    top_k: int = 5,
) -> tuple[RepositoryHistorySemanticHit, ...]:
    """Return the closest indexed history records for a natural-language query."""

    normalized_query = query.strip()

    if not repository.strip():
        raise ValueError("Repository must not be blank")
    if not normalized_query:
        raise ValueError("Search query cannot be empty")
    if not embedding_provider_name.strip() or not embedding_model.strip():
        raise ValueError("Embedding configuration cannot be blank")
    if type(embedding_dimensions) is not int or embedding_dimensions <= 0:
        raise ValueError("embedding_dimensions must be positive")
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    vector = await embedding_client.embed_query(normalized_query)
    _validate_vector(vector, embedding_dimensions)

    stored = RepositoryHistoryEmbedding
    distance = stored.embedding.cosine_distance(vector)

    statement = (
        select(stored, distance.label("distance"))
        .where(
            stored.repository == repository,
            stored.embedding_provider == embedding_provider_name,
            stored.embedding_model == embedding_model,
            stored.embedding_dimensions == embedding_dimensions,
        )
        .order_by(
            distance.asc(),
            stored.source_key.asc(),
        )
        .limit(top_k)
    )

    result = await session.execute(statement)

    return tuple(
        RepositoryHistorySemanticHit(
            repository=record.repository,
            number=record.number,
            source_type=record.source_type,
            comment_id=record.comment_id,
            title=record.title,
            content=record.content,
            source_url=record.source_url,
            source_updated_at=record.source_updated_at,
            distance=float(cosine_distance),
        )
        for record, cosine_distance in result.all()
    )
