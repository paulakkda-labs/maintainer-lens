"""Compare repository semantic and automatic-hybrid retrieval quality."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.evaluation.retrieval_anchor_resolution import (
    resolve_dataset_chunk_keys,
)
from maintainer_lens.evaluation.retrieval_dataset import RetrievalDataset
from maintainer_lens.evaluation.retrieval_metrics import (
    RetrievalCase as MetricsCase,
)
from maintainer_lens.evaluation.retrieval_metrics import (
    RetrievalMetricsSummary,
    evaluate_repository_retrieval,
)
from maintainer_lens.services.repository_search import search_repository
from maintainer_lens.services.repository_semantic_search import (
    search_repository_by_text,
)


class _CachedQueryEmbeddingProvider:
    """Reuse identical query vectors for both retrieval strategies."""

    def __init__(self, provider: EmbeddingProvider) -> None:
        self._provider = provider
        self._query_cache: dict[str, list[float]] = {}

    async def embed_query(self, text: str) -> list[float]:
        if text not in self._query_cache:
            self._query_cache[text] = await self._provider.embed_query(text)

        return self._query_cache[text].copy()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._provider.embed_documents(texts)


@dataclass(frozen=True, slots=True)
class RetrievalComparisonCase:
    """Preserve per-question rankings for inspection."""

    case_id: str
    language: str
    query: str
    relevant_chunk_keys: frozenset[str]
    semantic_chunk_keys: tuple[str, ...]
    automatic_hybrid_chunk_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetrievalComparison:
    """Hold rankings and aggregate metrics for both strategies."""

    cases: tuple[RetrievalComparisonCase, ...]
    semantic: RetrievalMetricsSummary
    automatic_hybrid: RetrievalMetricsSummary


async def compare_repository_retrieval(
    session: AsyncSession,
    embedding_provider_client: EmbeddingProvider,
    index_id: UUID,
    dataset: RetrievalDataset,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    top_k: int = 5,
    candidate_limit: int = 10,
    rank_constant: int = 60,
) -> RetrievalComparison:
    """Evaluate both strategies using the same queries and ground truth."""

    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    if not top_k <= candidate_limit <= 20:
        raise ValueError("candidate_limit must be between top_k and 20")

    if rank_constant <= 0:
        raise ValueError("rank_constant must be positive")

    relevant_by_case = await resolve_dataset_chunk_keys(session, index_id, dataset)

    cached_provider = _CachedQueryEmbeddingProvider(embedding_provider_client)

    comparisons: list[RetrievalComparisonCase] = []
    semantic_cases: list[MetricsCase] = []
    hybrid_cases: list[MetricsCase] = []

    for case in dataset.cases:
        relevant_keys = relevant_by_case.get(case.id)

        if not relevant_keys:
            raise ValueError(f"No resolved ground truth for case {case.id!r}")

        semantic_hits = await search_repository_by_text(
            session,
            cached_provider,
            index_id,
            case.query,
            embedding_provider_name=embedding_provider_name,
            embedding_model=embedding_model,
            top_k=top_k,
        )

        hybrid_hits = await search_repository(
            session,
            cached_provider,
            index_id,
            case.query,
            embedding_provider_name=embedding_provider_name,
            embedding_model=embedding_model,
            candidate_limit=candidate_limit,
            top_k=top_k,
            rank_constant=rank_constant,
        )

        if any(hit.index_id != index_id for hit in (*semantic_hits, *hybrid_hits)):
            raise ValueError("Retrieval result belongs to another repository index")

        semantic_keys = tuple(hit.chunk_key for hit in semantic_hits)
        hybrid_keys = tuple(hit.chunk_key for hit in hybrid_hits)

        comparisons.append(
            RetrievalComparisonCase(
                case_id=case.id,
                language=case.language,
                query=case.query,
                relevant_chunk_keys=relevant_keys,
                semantic_chunk_keys=semantic_keys,
                automatic_hybrid_chunk_keys=hybrid_keys,
            )
        )

        semantic_cases.append(
            MetricsCase(
                case.query,
                relevant_keys,
                semantic_keys,
            )
        )

        hybrid_cases.append(
            MetricsCase(
                case.query,
                relevant_keys,
                hybrid_keys,
            )
        )

    return RetrievalComparison(
        cases=tuple(comparisons),
        semantic=evaluate_repository_retrieval(semantic_cases, top_k=top_k),
        automatic_hybrid=evaluate_repository_retrieval(hybrid_cases, top_k=top_k),
    )
