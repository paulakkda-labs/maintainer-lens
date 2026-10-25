"""Evaluate repository retrieval recall and first-hit ranking quality."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    """A query with known relevant and retrieved chunk identities."""

    query: str
    relevant_chunk_keys: frozenset[str]
    retrieved_chunk_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QueryRetrievalMetrics:
    """Retrieval metrics for one query."""

    query: str
    relevant_count: int
    retrieved_relevant_count: int
    first_relevant_rank: int | None
    recall_at_k: float
    reciprocal_rank_at_k: float


@dataclass(frozen=True, slots=True)
class RetrievalMetricsSummary:
    """Aggregate retrieval metrics across multiple queries."""

    top_k: int
    query_count: int
    mean_recall_at_k: float
    mrr_at_k: float
    query_metrics: tuple[QueryRetrievalMetrics, ...]


def evaluate_repository_retrieval(
    cases: Sequence[RetrievalCase],
    *,
    top_k: int = 5,
) -> RetrievalMetricsSummary:
    """Evaluate ranked retrieval results against known relevant chunks."""

    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    if not cases:
        raise ValueError("Evaluation cases cannot be empty")

    query_metrics: list[QueryRetrievalMetrics] = []

    for case in cases:
        if not case.query.strip():
            raise ValueError("Evaluation query cannot be empty")

        if not case.relevant_chunk_keys:
            raise ValueError("Relevant chunk keys cannot be empty")

        if len(case.retrieved_chunk_keys) != len(set(case.retrieved_chunk_keys)):
            raise ValueError("Retrieved chunk keys contain duplicates")

        first_relevant_rank: int | None = None
        retrieved_relevant_count = 0

        for rank, chunk_key in enumerate(
            case.retrieved_chunk_keys[:top_k],
            start=1,
        ):
            if chunk_key not in case.relevant_chunk_keys:
                continue

            retrieved_relevant_count += 1

            if first_relevant_rank is None:
                first_relevant_rank = rank

        recall_at_k = retrieved_relevant_count / len(case.relevant_chunk_keys)

        reciprocal_rank_at_k = (
            1 / first_relevant_rank if first_relevant_rank is not None else 0.0
        )

        query_metrics.append(
            QueryRetrievalMetrics(
                query=case.query,
                relevant_count=len(case.relevant_chunk_keys),
                retrieved_relevant_count=retrieved_relevant_count,
                first_relevant_rank=first_relevant_rank,
                recall_at_k=recall_at_k,
                reciprocal_rank_at_k=reciprocal_rank_at_k,
            )
        )

    query_count = len(query_metrics)

    mean_recall_at_k = sum(item.recall_at_k for item in query_metrics) / query_count

    mrr_at_k = sum(item.reciprocal_rank_at_k for item in query_metrics) / query_count

    return RetrievalMetricsSummary(
        top_k=top_k,
        query_count=query_count,
        mean_recall_at_k=mean_recall_at_k,
        mrr_at_k=mrr_at_k,
        query_metrics=tuple(query_metrics),
    )
