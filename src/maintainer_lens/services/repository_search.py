"""Provide a natural-language entry point for repository retrieval."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.services.repository_hybrid_search import (
    HybridSearchResult,
    search_repository_hybrid,
)
from maintainer_lens.services.repository_query_analysis import (
    analyze_repository_query,
)


async def search_repository(
    session: AsyncSession,
    embedding_provider_client: EmbeddingProvider,
    index_id: UUID,
    query: str,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    candidate_limit: int = 10,
    top_k: int = 5,
    rank_constant: int = 60,
) -> list[HybridSearchResult]:
    """Search repository evidence using a natural-language question."""

    analysis = analyze_repository_query(query)

    return await search_repository_hybrid(
        session,
        embedding_provider_client,
        index_id,
        analysis.query,
        embedding_provider_name=embedding_provider_name,
        embedding_model=embedding_model,
        keyword=analysis.keyword,
        candidate_limit=candidate_limit,
        top_k=top_k,
        rank_constant=rank_constant,
    )
