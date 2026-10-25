"""Unit tests for the repository hybrid search orchestration."""

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from maintainer_lens.services.repository_hybrid_search import (
    HybridSearchResult,
    search_repository_hybrid,
)
from maintainer_lens.services.repository_keyword_search import (
    KeywordSearchResult,
)
from maintainer_lens.services.repository_vector_search import (
    RetrievedCodeChunk,
)

INDEX_ID = UUID("11111111-1111-1111-1111-111111111111")
MODEL = "qwen3.7-text-embedding-flash"

VECTOR_SEARCH_PATH = (
    "maintainer_lens.services.repository_hybrid_search.search_repository_by_text"
)
KEYWORD_SEARCH_PATH = (
    "maintainer_lens.services.repository_hybrid_search.search_repository_by_keyword"
)


def make_vector_hit(
    key: str,
    *,
    index_id: UUID = INDEX_ID,
) -> RetrievedCodeChunk:
    """Create a semantic retrieval result with realistic provenance."""

    return RetrievedCodeChunk(
        index_id=index_id,
        chunk_key=key,
        repository="pypa/sampleproject",
        commit_sha="a" * 40,
        file_path=f"src/{key}.py",
        blob_sha="b" * 40,
        language="python",
        kind="function",
        symbol=key,
        start_line=1,
        end_line=3,
        content=f"def {key}():\n    pass\n",
        distance=0.2,
    )


def make_keyword_hit(
    key: str,
    *,
    index_id: UUID = INDEX_ID,
) -> KeywordSearchResult:
    """Create a lexical retrieval result with realistic provenance."""

    return KeywordSearchResult(
        index_id=index_id,
        chunk_key=key,
        repository="pypa/sampleproject",
        commit_sha="a" * 40,
        file_path=f"src/{key}.py",
        blob_sha="b" * 40,
        language="python",
        kind="function",
        symbol=key,
        start_line=1,
        end_line=3,
        content=f"def {key}():\n    pass\n",
        matched_in="symbol",
    )


@pytest.mark.anyio
async def test_fuses_both_retrievers_and_preserves_evidence() -> None:
    """Combine both ranked lists and return unified source evidence."""

    vector_hits = [
        make_vector_hit("A"),
        make_vector_hit("C"),
        make_vector_hit("B"),
    ]

    keyword_hits = [
        make_keyword_hit("B"),
        make_keyword_hit("D"),
        make_keyword_hit("C"),
    ]

    session = object()
    provider = object()

    with (
        patch(VECTOR_SEARCH_PATH, new_callable=AsyncMock) as vector_mock,
        patch(KEYWORD_SEARCH_PATH, new_callable=AsyncMock) as keyword_mock,
    ):
        vector_mock.return_value = vector_hits
        keyword_mock.return_value = keyword_hits

        results = await search_repository_hybrid(
            session,
            provider,
            INDEX_ID,
            "  哪里处理 CancelledError？  ",
            embedding_provider_name="dashscope",
            embedding_model=MODEL,
            keyword="  CancelledError  ",
            candidate_limit=10,
            top_k=4,
        )

    vector_mock.assert_awaited_once_with(
        session,
        provider,
        INDEX_ID,
        "哪里处理 CancelledError？",
        embedding_provider_name="dashscope",
        embedding_model=MODEL,
        top_k=10,
    )

    keyword_mock.assert_awaited_once_with(
        session,
        INDEX_ID,
        "CancelledError",
        top_k=10,
    )

    assert all(isinstance(item, HybridSearchResult) for item in results)

    assert [item.chunk_key for item in results] == [
        "B",
        "C",
        "A",
        "D",
    ]

    by_key = {item.chunk_key: item for item in results}

    assert by_key["B"].vector_rank == 3
    assert by_key["B"].keyword_rank == 1
    assert by_key["B"].rrf_score == pytest.approx(1 / 63 + 1 / 61)
    assert by_key["B"].cosine_distance == pytest.approx(0.2)
    assert by_key["B"].matched_in == "symbol"

    assert by_key["A"].vector_rank == 1
    assert by_key["A"].keyword_rank is None
    assert by_key["A"].cosine_distance == pytest.approx(0.2)
    assert by_key["A"].matched_in is None

    assert by_key["D"].vector_rank is None
    assert by_key["D"].keyword_rank == 2
    assert by_key["D"].cosine_distance is None
    assert by_key["D"].matched_in == "symbol"

    for item in results:
        assert item.index_id == INDEX_ID
        assert item.repository == "pypa/sampleproject"
        assert item.commit_sha == "a" * 40
        assert item.file_path == f"src/{item.chunk_key}.py"
        assert item.blob_sha == "b" * 40
        assert item.start_line == 1
        assert item.end_line == 3
        assert item.content


@pytest.mark.anyio
async def test_works_without_explicit_keyword() -> None:
    """Run semantic-only retrieval when no reliable keyword is supplied."""

    with (
        patch(VECTOR_SEARCH_PATH, new_callable=AsyncMock) as vector_mock,
        patch(KEYWORD_SEARCH_PATH, new_callable=AsyncMock) as keyword_mock,
    ):
        vector_mock.return_value = [
            make_vector_hit("A"),
            make_vector_hit("B"),
        ]

        results = await search_repository_hybrid(
            object(),
            object(),
            INDEX_ID,
            "哪个函数负责处理异步请求取消？",
            embedding_provider_name="dashscope",
            embedding_model=MODEL,
            top_k=1,
        )

    assert [item.chunk_key for item in results] == ["A"]
    assert results[0].vector_rank == 1
    assert results[0].keyword_rank is None
    assert results[0].rrf_score == pytest.approx(1 / 61)

    vector_mock.assert_awaited_once()
    keyword_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_supports_keyword_only_candidates() -> None:
    """Keep keyword matches even when semantic retrieval has no results."""

    with (
        patch(VECTOR_SEARCH_PATH, new_callable=AsyncMock) as vector_mock,
        patch(KEYWORD_SEARCH_PATH, new_callable=AsyncMock) as keyword_mock,
    ):
        vector_mock.return_value = []
        keyword_mock.return_value = [
            make_keyword_hit("B"),
            make_keyword_hit("D"),
        ]

        results = await search_repository_hybrid(
            object(),
            object(),
            INDEX_ID,
            "Where is CancelledError handled?",
            embedding_provider_name="dashscope",
            embedding_model=MODEL,
            keyword="CancelledError",
        )

    assert [item.chunk_key for item in results] == ["B", "D"]
    assert [item.keyword_rank for item in results] == [1, 2]
    assert all(item.vector_rank is None for item in results)


@pytest.mark.anyio
async def test_returns_empty_when_both_retrievers_are_empty() -> None:
    """Return no hits when neither retriever finds candidates."""

    with (
        patch(VECTOR_SEARCH_PATH, new_callable=AsyncMock) as vector_mock,
        patch(KEYWORD_SEARCH_PATH, new_callable=AsyncMock) as keyword_mock,
    ):
        vector_mock.return_value = []
        keyword_mock.return_value = []

        results = await search_repository_hybrid(
            object(),
            object(),
            INDEX_ID,
            "unknown function",
            embedding_provider_name="dashscope",
            embedding_model=MODEL,
            keyword="unknown_identifier",
        )

    assert results == []


@pytest.mark.anyio
async def test_rejects_foreign_repository_index_results() -> None:
    """Never combine candidates from different repository indexes."""

    with (
        patch(VECTOR_SEARCH_PATH, new_callable=AsyncMock) as vector_mock,
        patch(KEYWORD_SEARCH_PATH, new_callable=AsyncMock) as keyword_mock,
    ):
        vector_mock.return_value = [make_vector_hit("A", index_id=uuid4())]
        keyword_mock.return_value = []

        with pytest.raises(ValueError, match="another repository index"):
            await search_repository_hybrid(
                object(),
                object(),
                INDEX_ID,
                "query",
                embedding_provider_name="dashscope",
                embedding_model=MODEL,
                keyword="symbol",
            )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "keyword", "candidate_limit", "top_k", "rank_constant"),
    [
        ("   ", None, 10, 5, 60),
        ("query", "   ", 10, 5, 60),
        ("query", "a" * 129, 10, 5, 60),
        ("query", None, 0, 5, 60),
        ("query", None, 21, 5, 60),
        ("query", None, 10, 0, 60),
        ("query", None, 10, 21, 60),
        ("query", None, 10, 5, 0),
    ],
)
async def test_rejects_invalid_arguments_before_retrieval(
    query: str,
    keyword: str | None,
    candidate_limit: int,
    top_k: int,
    rank_constant: int,
) -> None:
    """Reject invalid inputs before external or database requests."""

    with (
        patch(VECTOR_SEARCH_PATH, new_callable=AsyncMock) as vector_mock,
        patch(KEYWORD_SEARCH_PATH, new_callable=AsyncMock) as keyword_mock,
    ):
        with pytest.raises(ValueError):
            await search_repository_hybrid(
                object(),
                object(),
                INDEX_ID,
                query,
                embedding_provider_name="dashscope",
                embedding_model=MODEL,
                keyword=keyword,
                candidate_limit=candidate_limit,
                top_k=top_k,
                rank_constant=rank_constant,
            )

    vector_mock.assert_not_awaited()
    keyword_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_semantic_failure_does_not_run_keyword_retrieval() -> None:
    """Propagate semantic retrieval errors instead of hiding failures."""

    with (
        patch(VECTOR_SEARCH_PATH, new_callable=AsyncMock) as vector_mock,
        patch(KEYWORD_SEARCH_PATH, new_callable=AsyncMock) as keyword_mock,
    ):
        vector_mock.side_effect = RuntimeError("Embedding service unavailable")

        with pytest.raises(
            RuntimeError,
            match="Embedding service unavailable",
        ):
            await search_repository_hybrid(
                object(),
                object(),
                INDEX_ID,
                "Find CancelledError handling",
                embedding_provider_name="dashscope",
                embedding_model=MODEL,
                keyword="CancelledError",
            )

    keyword_mock.assert_not_awaited()
