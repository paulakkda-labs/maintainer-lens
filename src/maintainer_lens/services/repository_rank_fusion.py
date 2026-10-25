"""Fuse repository retrieval rankings using reciprocal rank fusion."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RRFHit:
    chunk_key: str
    score: float
    vector_rank: int | None
    keyword_rank: int | None


def fuse_repository_rankings(
    vector_chunk_keys: Sequence[str],
    keyword_chunk_keys: Sequence[str],
    *,
    rank_constant: int = 60,
    top_k: int = 5,
) -> list[RRFHit]:
    """Fuse vector and keyword rankings using reciprocal rank fusion."""

    if rank_constant <= 0:
        raise ValueError("rank_constant must be positive")

    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")

    if len(vector_chunk_keys) != len(set(vector_chunk_keys)):
        raise ValueError("duplicate chunk keys in vector rankings")

    if len(keyword_chunk_keys) != len(set(keyword_chunk_keys)):
        raise ValueError("duplicate chunk keys in keyword rankings")

    vector_ranks = {
        chunk_key: rank for rank, chunk_key in enumerate(vector_chunk_keys, start=1)
    }

    keyword_ranks = {
        chunk_key: rank for rank, chunk_key in enumerate(keyword_chunk_keys, start=1)
    }

    all_chunk_keys = set(vector_ranks) | set(keyword_ranks)

    results: list[RRFHit] = []

    for chunk_key in all_chunk_keys:
        vector_rank = vector_ranks.get(chunk_key)
        keyword_rank = keyword_ranks.get(chunk_key)

        score = 0.0

        if vector_rank is not None:
            score += 1 / (rank_constant + vector_rank)

        if keyword_rank is not None:
            score += 1 / (rank_constant + keyword_rank)

        results.append(
            RRFHit(
                chunk_key=chunk_key,
                score=score,
                vector_rank=vector_rank,
                keyword_rank=keyword_rank,
            )
        )

    results.sort(key=lambda item: (-item.score, item.chunk_key))

    return results[:top_k]
