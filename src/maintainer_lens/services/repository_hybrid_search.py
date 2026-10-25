"""Combine semantic and keyword repository retrieval using RRF."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.services.repository_keyword_search import (
    KeywordSearchResult,
    search_repository_by_keyword,
)
from maintainer_lens.services.repository_rank_fusion import (
    fuse_repository_rankings,
)
from maintainer_lens.services.repository_semantic_search import (
    search_repository_by_text,
)
from maintainer_lens.services.repository_vector_search import (
    RetrievedCodeChunk,
)


@dataclass(frozen=True, slots=True)
class HybridSearchResult:
    """A fused retrieval hit with source provenance and ranking details."""

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

    rrf_score: float
    vector_rank: int | None
    keyword_rank: int | None
    cosine_distance: float | None
    matched_in: str | None


async def search_repository_hybrid(
    session: AsyncSession,
    embedding_provider_client: EmbeddingProvider,
    index_id: UUID,
    query: str,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    keyword: str | None = None,
    candidate_limit: int = 10,
    top_k: int = 5,
    rank_constant: int = 60,
) -> list[HybridSearchResult]:
    """Fuse semantic and optional literal keyword retrieval results."""

    normalized_query = query.strip()

    if not normalized_query:
        raise ValueError("Search query cannot be empty")

    if not 1 <= candidate_limit <= 20:
        raise ValueError("candidate_limit must be between 1 and 20")

    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    if rank_constant <= 0:
        raise ValueError("rank_constant must be positive")

    normalized_keyword = None

    if keyword is not None:
        normalized_keyword = keyword.strip()

        if not normalized_keyword:
            raise ValueError("Search keyword cannot be empty")

        if len(normalized_keyword) > 128:
            raise ValueError("Search keyword is too long")

    # First, retrieve semantic candidates using query embeddings.
    vector_results = await search_repository_by_text(
        session,
        embedding_provider_client,
        index_id,
        normalized_query,
        embedding_provider_name=embedding_provider_name,
        embedding_model=embedding_model,
        top_k=candidate_limit,
    )

    # Then, optionally retrieve literal keyword candidates.
    keyword_results: list[KeywordSearchResult] = []

    if normalized_keyword is not None:
        keyword_results = await search_repository_by_keyword(
            session,
            index_id,
            normalized_keyword,
            top_k=candidate_limit,
        )

    # Both retrievers must return chunks from the requested index.
    for item in (*vector_results, *keyword_results):
        if item.index_id != index_id:
            raise ValueError("Retrieval result belongs to another repository index")

    # Fuse rankings, not raw similarity or keyword scores.
    fused_hits = fuse_repository_rankings(
        [item.chunk_key for item in vector_results],
        [item.chunk_key for item in keyword_results],
        rank_constant=rank_constant,
        top_k=top_k,
    )

    vector_by_key: dict[str, RetrievedCodeChunk] = {
        item.chunk_key: item for item in vector_results
    }

    keyword_by_key: dict[str, KeywordSearchResult] = {
        item.chunk_key: item for item in keyword_results
    }

    results: list[HybridSearchResult] = []

    for hit in fused_hits:
        vector_item = vector_by_key.get(hit.chunk_key)
        keyword_item = keyword_by_key.get(hit.chunk_key)

        source = vector_item if vector_item is not None else keyword_item

        if source is None:
            raise RuntimeError("Fused chunk is missing from retrieval results")

        results.append(
            HybridSearchResult(
                index_id=source.index_id,
                chunk_key=source.chunk_key,
                repository=source.repository,
                commit_sha=source.commit_sha,
                file_path=source.file_path,
                blob_sha=source.blob_sha,
                language=source.language,
                kind=source.kind,
                symbol=source.symbol,
                start_line=source.start_line,
                end_line=source.end_line,
                content=source.content,
                rrf_score=hit.score,
                vector_rank=hit.vector_rank,
                keyword_rank=hit.keyword_rank,
                cosine_distance=(
                    vector_item.distance if vector_item is not None else None
                ),
                matched_in=(
                    keyword_item.matched_in if keyword_item is not None else None
                ),
            )
        )

    return results
