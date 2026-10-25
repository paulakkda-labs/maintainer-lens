"""Unit tests for repository retrieval evaluation metrics."""

import pytest

from maintainer_lens.evaluation.retrieval_metrics import (
    RetrievalCase,
    evaluate_repository_retrieval,
)


def test_evaluates_multiple_queries() -> None:
    """Calculate per-query recall, reciprocal rank, and their averages."""

    cases = (
        RetrievalCase(
            query="Find A and C",
            relevant_chunk_keys=frozenset({"A", "C"}),
            retrieved_chunk_keys=("A", "B", "C"),
        ),
        RetrievalCase(
            query="Find X",
            relevant_chunk_keys=frozenset({"X"}),
            retrieved_chunk_keys=("Y", "X", "Z"),
        ),
        RetrievalCase(
            query="Find P",
            relevant_chunk_keys=frozenset({"P"}),
            retrieved_chunk_keys=("Q", "R", "S"),
        ),
    )

    summary = evaluate_repository_retrieval(cases, top_k=2)

    assert summary.top_k == 2
    assert summary.query_count == 3

    first, second, third = summary.query_metrics

    # Query 1: one of two relevant chunks is retrieved at rank 1.
    assert first.relevant_count == 2
    assert first.retrieved_relevant_count == 1
    assert first.first_relevant_rank == 1
    assert first.recall_at_k == pytest.approx(0.5)
    assert first.reciprocal_rank_at_k == pytest.approx(1.0)

    # Query 2: the only relevant chunk is retrieved at rank 2.
    assert second.relevant_count == 1
    assert second.retrieved_relevant_count == 1
    assert second.first_relevant_rank == 2
    assert second.recall_at_k == pytest.approx(1.0)
    assert second.reciprocal_rank_at_k == pytest.approx(0.5)

    # Query 3: no relevant chunk appears in the top 2.
    assert third.retrieved_relevant_count == 0
    assert third.first_relevant_rank is None
    assert third.recall_at_k == pytest.approx(0.0)
    assert third.reciprocal_rank_at_k == pytest.approx(0.0)

    assert summary.mean_recall_at_k == pytest.approx(0.5)
    assert summary.mrr_at_k == pytest.approx(0.5)


def test_recall_changes_with_cutoff() -> None:
    """Increasing K can recover more relevant chunks."""

    case = RetrievalCase(
        query="Find A and C",
        relevant_chunk_keys=frozenset({"A", "C"}),
        retrieved_chunk_keys=("B", "A", "C"),
    )

    top_two = evaluate_repository_retrieval([case], top_k=2)
    top_three = evaluate_repository_retrieval([case], top_k=3)

    assert top_two.mean_recall_at_k == pytest.approx(0.5)
    assert top_three.mean_recall_at_k == pytest.approx(1.0)

    # The first relevant hit stays at rank 2.
    assert top_two.mrr_at_k == pytest.approx(0.5)
    assert top_three.mrr_at_k == pytest.approx(0.5)


def test_relevant_result_beyond_cutoff_does_not_count() -> None:
    """Ignore correct results appearing after the evaluation cutoff."""

    case = RetrievalCase(
        query="Find target",
        relevant_chunk_keys=frozenset({"target"}),
        retrieved_chunk_keys=("A", "B", "target"),
    )

    summary = evaluate_repository_retrieval([case], top_k=2)

    assert summary.mean_recall_at_k == 0.0
    assert summary.mrr_at_k == 0.0
    assert summary.query_metrics[0].first_relevant_rank is None


def test_empty_retrieval_results_score_zero() -> None:
    """A valid query may return no candidates."""

    case = RetrievalCase(
        query="Find A",
        relevant_chunk_keys=frozenset({"A"}),
        retrieved_chunk_keys=(),
    )

    summary = evaluate_repository_retrieval([case])

    assert summary.mean_recall_at_k == 0.0
    assert summary.mrr_at_k == 0.0


def test_rejects_duplicate_retrieved_chunks() -> None:
    """Do not allow one chunk to occupy multiple result positions."""

    case = RetrievalCase(
        query="Find A",
        relevant_chunk_keys=frozenset({"A"}),
        retrieved_chunk_keys=("A", "B", "A"),
    )

    with pytest.raises(ValueError, match="duplicates"):
        evaluate_repository_retrieval([case])


@pytest.mark.parametrize("top_k", [0, -1, 21])
def test_rejects_invalid_top_k(top_k: int) -> None:
    """Reject evaluation cutoffs outside the supported range."""

    case = RetrievalCase(
        query="Find A",
        relevant_chunk_keys=frozenset({"A"}),
        retrieved_chunk_keys=("A",),
    )

    with pytest.raises(ValueError, match="top_k"):
        evaluate_repository_retrieval([case], top_k=top_k)


def test_rejects_empty_evaluation_dataset() -> None:
    """Require at least one query for meaningful aggregate metrics."""

    with pytest.raises(ValueError, match="cannot be empty"):
        evaluate_repository_retrieval([])


def test_rejects_blank_evaluation_query() -> None:
    """Reject evaluation cases without a meaningful question."""

    case = RetrievalCase(
        query="   ",
        relevant_chunk_keys=frozenset({"A"}),
        retrieved_chunk_keys=("A",),
    )

    with pytest.raises(ValueError, match="query cannot be empty"):
        evaluate_repository_retrieval([case])


def test_rejects_case_without_known_relevant_chunks() -> None:
    """Recall is undefined when no relevant documents are annotated."""

    case = RetrievalCase(
        query="Unknown issue",
        relevant_chunk_keys=frozenset(),
        retrieved_chunk_keys=("A", "B"),
    )

    with pytest.raises(ValueError, match="Relevant chunk keys"):
        evaluate_repository_retrieval([case])
