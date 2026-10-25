"""Unit tests for reciprocal rank fusion."""

import pytest

from maintainer_lens.services.repository_rank_fusion import (
    RRFHit,
    fuse_repository_rankings,
)


def test_fuses_vector_and_keyword_rankings() -> None:
    """Combine rankings, deduplicate chunks, and calculate RRF scores."""

    results = fuse_repository_rankings(
        ["A", "C", "B"],
        ["B", "D", "C"],
        rank_constant=60,
        top_k=4,
    )

    assert len(results) == 4
    assert all(isinstance(item, RRFHit) for item in results)

    assert [item.chunk_key for item in results] == [
        "B",
        "C",
        "A",
        "D",
    ]

    by_key = {item.chunk_key: item for item in results}

    assert by_key["B"].vector_rank == 3
    assert by_key["B"].keyword_rank == 1
    assert by_key["B"].score == pytest.approx(1 / 63 + 1 / 61)

    assert by_key["C"].vector_rank == 2
    assert by_key["C"].keyword_rank == 3
    assert by_key["C"].score == pytest.approx(1 / 62 + 1 / 63)

    assert by_key["A"].vector_rank == 1
    assert by_key["A"].keyword_rank is None
    assert by_key["A"].score == pytest.approx(1 / 61)

    assert by_key["D"].vector_rank is None
    assert by_key["D"].keyword_rank == 2
    assert by_key["D"].score == pytest.approx(1 / 62)


def test_respects_top_k() -> None:
    """Return only the requested number of fused candidates."""

    results = fuse_repository_rankings(
        ["A", "B", "C"],
        [],
        top_k=2,
    )

    assert [item.chunk_key for item in results] == ["A", "B"]


def test_keyword_only_rankings() -> None:
    """Support a search with no vector candidates."""

    results = fuse_repository_rankings(
        [],
        ["B", "A"],
        top_k=5,
    )

    assert [item.chunk_key for item in results] == ["B", "A"]

    assert all(item.vector_rank is None for item in results)
    assert [item.keyword_rank for item in results] == [1, 2]


def test_empty_rankings() -> None:
    """Return an empty result when no candidates exist."""

    assert fuse_repository_rankings([], []) == []


def test_same_chunk_is_counted_once_per_retriever() -> None:
    """A chunk present in both rankings must appear only once."""

    results = fuse_repository_rankings(
        ["A", "B"],
        ["B", "A"],
        top_k=2,
    )

    assert len(results) == 2
    assert {item.chunk_key for item in results} == {"A", "B"}

    for item in results:
        assert item.score == pytest.approx(1 / 61 + 1 / 62)


def test_equal_scores_use_deterministic_key_order() -> None:
    """Break equal-score ties with the chunk key."""

    results = fuse_repository_rankings(
        ["B", "A"],
        ["A", "B"],
        top_k=2,
    )

    assert [item.chunk_key for item in results] == ["A", "B"]
    assert results[0].score == pytest.approx(results[1].score)


def test_rank_constant_changes_scores() -> None:
    """Apply the provided RRF rank constant."""

    results = fuse_repository_rankings(
        ["A"],
        ["A"],
        rank_constant=1,
    )

    assert len(results) == 1
    assert results[0].score == pytest.approx(1.0)


@pytest.mark.parametrize("rank_constant", [0, -1])
def test_rejects_invalid_rank_constant(
    rank_constant: int,
) -> None:
    """RRF rank constant must be positive."""

    with pytest.raises(ValueError, match="rank_constant"):
        fuse_repository_rankings(
            ["A"],
            ["B"],
            rank_constant=rank_constant,
        )


@pytest.mark.parametrize("top_k", [0, -1, 21])
def test_rejects_invalid_top_k(top_k: int) -> None:
    """Reject result limits outside the supported range."""

    with pytest.raises(ValueError, match="top_k"):
        fuse_repository_rankings(
            ["A"],
            ["B"],
            top_k=top_k,
        )


@pytest.mark.parametrize(
    ("vector_keys", "keyword_keys"),
    [
        (["A", "A"], ["B"]),
        (["A"], ["B", "B"]),
    ],
)
def test_rejects_duplicate_keys_within_one_ranking(
    vector_keys: list[str],
    keyword_keys: list[str],
) -> None:
    """Repeated keys inside one retriever ranking are invalid."""

    with pytest.raises(ValueError, match="duplicate"):
        fuse_repository_rankings(
            vector_keys,
            keyword_keys,
        )
