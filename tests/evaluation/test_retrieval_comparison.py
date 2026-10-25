"""Tests for semantic versus automatic-hybrid retrieval evaluation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from maintainer_lens.evaluation import retrieval_comparison as comparison
from maintainer_lens.evaluation.retrieval_dataset import RetrievalDataset

INDEX_ID = UUID("00000000-0000-0000-0000-000000000001")
WRONG_INDEX_ID = UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _dataset() -> RetrievalDataset:
    return RetrievalDataset.model_validate(
        {
            "schema_version": 1,
            "repository": "pypa/sampleproject",
            "commit_sha": "a" * 40,
            "cases": [
                {
                    "id": "natural-en",
                    "language": "en",
                    "query": "Which function adds one to a number?",
                    "relevant_evidence": [
                        {
                            "file_path": "src/sample/simple.py",
                            "start_line": 1,
                            "end_line": 2,
                            "required_text": "return number + 1",
                        }
                    ],
                },
                {
                    "id": "identifier-zh",
                    "language": "zh",
                    "query": "在哪里定义 `main()`？",
                    "relevant_evidence": [
                        {
                            "file_path": "src/sample/__init__.py",
                            "start_line": 1,
                            "end_line": 2,
                            "required_text": "def main",
                        }
                    ],
                },
            ],
        }
    )


def _hit(key: str, *, index_id: UUID = INDEX_ID) -> SimpleNamespace:
    return SimpleNamespace(
        index_id=index_id,
        chunk_key=key,
    )


@pytest.mark.anyio
async def test_compares_identical_queries_with_shared_embeddings(
    monkeypatch,
) -> None:
    resolver = AsyncMock(
        return_value={
            "natural-en": frozenset({"add-one"}),
            "identifier-zh": frozenset({"main"}),
        }
    )
    monkeypatch.setattr(comparison, "resolve_dataset_chunk_keys", resolver)

    semantic_rankings = [
        [_hit("noise"), _hit("add-one")],
        [_hit("noise")],
    ]
    hybrid_rankings = [
        [_hit("noise"), _hit("add-one")],
        [_hit("main"), _hit("noise")],
    ]

    semantic_vectors: list[list[float]] = []
    hybrid_vectors: list[list[float]] = []

    async def fake_semantic(session, provider, index_id, query, **kwargs):
        semantic_vectors.append(await provider.embed_query(query))
        return semantic_rankings[len(semantic_vectors) - 1]

    async def fake_hybrid(session, provider, index_id, query, **kwargs):
        hybrid_vectors.append(await provider.embed_query(query))
        return hybrid_rankings[len(hybrid_vectors) - 1]

    semantic_search = AsyncMock(side_effect=fake_semantic)
    hybrid_search = AsyncMock(side_effect=fake_hybrid)

    monkeypatch.setattr(comparison, "search_repository_by_text", semantic_search)
    monkeypatch.setattr(comparison, "search_repository", hybrid_search)

    provider = AsyncMock()
    provider.embed_query.side_effect = [
        [1.0, 0.0],
        [0.0, 1.0],
    ]

    session = object()
    dataset = _dataset()

    result = await comparison.compare_repository_retrieval(
        session,
        provider,
        INDEX_ID,
        dataset,
        embedding_provider_name="test-provider",
        embedding_model="test-model",
        top_k=2,
        candidate_limit=5,
        rank_constant=60,
    )

    assert result.semantic.mean_recall_at_k == pytest.approx(0.5)
    assert result.semantic.mrr_at_k == pytest.approx(0.25)

    assert result.automatic_hybrid.mean_recall_at_k == pytest.approx(1.0)
    assert result.automatic_hybrid.mrr_at_k == pytest.approx(0.75)

    assert [case.case_id for case in result.cases] == [
        "natural-en",
        "identifier-zh",
    ]

    assert result.cases[1].semantic_chunk_keys == ("noise",)
    assert result.cases[1].automatic_hybrid_chunk_keys == ("main", "noise")

    assert (
        semantic_vectors
        == hybrid_vectors
        == [
            [1.0, 0.0],
            [0.0, 1.0],
        ]
    )

    assert provider.embed_query.await_count == 2

    resolver.assert_awaited_once_with(session, INDEX_ID, dataset)

    assert semantic_search.await_count == 2
    assert hybrid_search.await_count == 2

    assert semantic_search.await_args_list[0].kwargs["top_k"] == 2
    assert hybrid_search.await_args_list[0].kwargs["candidate_limit"] == 5
    assert hybrid_search.await_args_list[0].kwargs["rank_constant"] == 60


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("top_k", "candidate_limit", "rank_constant", "message"),
    [
        (0, 10, 60, "top_k"),
        (3, 2, 60, "candidate_limit"),
        (2, 10, 0, "rank_constant"),
    ],
)
async def test_invalid_options_fail_before_database_work(
    monkeypatch,
    top_k: int,
    candidate_limit: int,
    rank_constant: int,
    message: str,
) -> None:
    resolver = AsyncMock()

    monkeypatch.setattr(comparison, "resolve_dataset_chunk_keys", resolver)

    with pytest.raises(ValueError, match=message):
        await comparison.compare_repository_retrieval(
            object(),
            AsyncMock(),
            INDEX_ID,
            _dataset(),
            embedding_provider_name="test-provider",
            embedding_model="test-model",
            top_k=top_k,
            candidate_limit=candidate_limit,
            rank_constant=rank_constant,
        )

    resolver.assert_not_awaited()


@pytest.mark.anyio
async def test_missing_ground_truth_stops_search(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        comparison,
        "resolve_dataset_chunk_keys",
        AsyncMock(return_value={"natural-en": frozenset()}),
    )

    semantic_search = AsyncMock()
    monkeypatch.setattr(comparison, "search_repository_by_text", semantic_search)

    with pytest.raises(ValueError, match="No resolved ground truth"):
        await comparison.compare_repository_retrieval(
            object(),
            AsyncMock(),
            INDEX_ID,
            _dataset(),
            embedding_provider_name="test-provider",
            embedding_model="test-model",
        )

    semantic_search.assert_not_awaited()


@pytest.mark.anyio
async def test_rejects_cross_index_search_results(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        comparison,
        "resolve_dataset_chunk_keys",
        AsyncMock(
            return_value={
                "natural-en": frozenset({"add-one"}),
                "identifier-zh": frozenset({"main"}),
            }
        ),
    )

    monkeypatch.setattr(
        comparison,
        "search_repository_by_text",
        AsyncMock(return_value=[_hit("add-one", index_id=WRONG_INDEX_ID)]),
    )

    monkeypatch.setattr(
        comparison,
        "search_repository",
        AsyncMock(return_value=[_hit("add-one")]),
    )

    with pytest.raises(ValueError, match="another repository index"):
        await comparison.compare_repository_retrieval(
            object(),
            AsyncMock(),
            INDEX_ID,
            _dataset(),
            embedding_provider_name="test-provider",
            embedding_model="test-model",
        )


@pytest.mark.anyio
async def test_search_errors_propagate(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        comparison,
        "resolve_dataset_chunk_keys",
        AsyncMock(
            return_value={
                "natural-en": frozenset({"add-one"}),
                "identifier-zh": frozenset({"main"}),
            }
        ),
    )

    monkeypatch.setattr(
        comparison,
        "search_repository_by_text",
        AsyncMock(side_effect=RuntimeError("Embedding API unavailable")),
    )

    hybrid_search = AsyncMock()
    monkeypatch.setattr(comparison, "search_repository", hybrid_search)

    with pytest.raises(RuntimeError, match="Embedding API unavailable"):
        await comparison.compare_repository_retrieval(
            object(),
            AsyncMock(),
            INDEX_ID,
            _dataset(),
            embedding_provider_name="test-provider",
            embedding_model="test-model",
        )

    hybrid_search.assert_not_awaited()
