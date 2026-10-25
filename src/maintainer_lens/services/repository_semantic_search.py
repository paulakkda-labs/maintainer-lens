"""Search repository source code using natural-language queries."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.services.repository_vector_search import (
    RepositoryIndexNotFound,
    RepositoryIndexNotReady,
    RetrievedCodeChunk,
    search_repository_chunks,
)


async def search_repository_by_text(
    session: AsyncSession,
    embedding_provider_client: EmbeddingProvider,
    index_id: UUID,
    query: str,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    top_k: int = 5,
) -> list[RetrievedCodeChunk]:
    """Search a repository index using a multilingual text query."""

    normalized_query = query.strip()

    if not normalized_query:
        raise ValueError("Search query cannot be empty")

    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    if not embedding_provider_name.strip() or not embedding_model.strip():
        raise ValueError("Embedding configuration cannot be empty")

    async with session.begin():
        index = await session.get(RepositoryIndex, index_id)

        if index is None:
            raise RepositoryIndexNotFound(f"Repository index {index_id} does not exist")

        if index.status != "ready":
            raise RepositoryIndexNotReady(f"Repository index {index_id} is not ready")

        if (
            index.embedding_provider != embedding_provider_name
            or index.embedding_model != embedding_model
        ):
            raise ValueError(
                "Query embedding configuration does not match repository index"
            )

    query_vector = await embedding_provider_client.embed_query(normalized_query)

    return await search_repository_chunks(
        session,
        index_id,
        query_vector,
        top_k=top_k,
    )
