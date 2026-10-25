"""Tests for the natural-language repository search entry point."""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from maintainer_lens.services.repository_search import (
    search_repository,
)

INDEX_ID = UUID("11111111-1111-1111-1111-111111111111")

HYBRID_SEARCH_PATH = (
    "maintainer_lens.services.repository_search.search_repository_hybrid"
)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "expected_query", "expected_keyword"),
    [
        (
            "  哪里处理 `asyncio.CancelledError`？  ",
            "哪里处理 `asyncio.CancelledError`？",
            "asyncio.CancelledError",
        ),
        (
            "Where is AsyncClient.send() defined?",
            "Where is AsyncClient.send() defined?",
            "AsyncClient.send",
        ),
        (
            "哪个函数会把传入的数字加一？",
            "哪个函数会把传入的数字加一？",
            None,
        ),
        (
            "Which function adds one to a number?",
            "Which function adds one to a number?",
            None,
        ),
    ],
)
async def test_automatically_passes_analyzed_query_to_hybrid_search(
    query: str,
    expected_query: str,
    expected_keyword: str | None,
) -> None:
    """Forward normalized text and an optional extracted identifier."""

    session = object()
    provider = object()
    expected_results = [object()]

    with patch(
        HYBRID_SEARCH_PATH,
        new_callable=AsyncMock,
    ) as hybrid_mock:
        hybrid_mock.return_value = expected_results

        results = await search_repository(
            session,
            provider,
            INDEX_ID,
            query,
            embedding_provider_name="dashscope",
            embedding_model="test-model",
        )

    assert results is expected_results

    hybrid_mock.assert_awaited_once_with(
        session,
        provider,
        INDEX_ID,
        expected_query,
        embedding_provider_name="dashscope",
        embedding_model="test-model",
        keyword=expected_keyword,
        candidate_limit=10,
        top_k=5,
        rank_constant=60,
    )


@pytest.mark.anyio
async def test_forwards_custom_retrieval_limits() -> None:
    """Preserve custom candidate, final-result, and RRF limits."""

    with patch(
        HYBRID_SEARCH_PATH,
        new_callable=AsyncMock,
        return_value=[],
    ) as hybrid_mock:
        results = await search_repository(
            object(),
            object(),
            INDEX_ID,
            "哪里处理 CancelledError？",
            embedding_provider_name="dashscope",
            embedding_model="test-model",
            candidate_limit=12,
            top_k=4,
            rank_constant=30,
        )

    assert results == []

    assert hybrid_mock.await_args.kwargs["keyword"] == "CancelledError"
    assert hybrid_mock.await_args.kwargs["candidate_limit"] == 12
    assert hybrid_mock.await_args.kwargs["top_k"] == 4
    assert hybrid_mock.await_args.kwargs["rank_constant"] == 30


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
async def test_rejects_blank_query_before_hybrid_search(
    query: str,
) -> None:
    """Reject blank queries without executing downstream retrieval."""

    with patch(
        HYBRID_SEARCH_PATH,
        new_callable=AsyncMock,
    ) as hybrid_mock:
        with pytest.raises(ValueError, match="cannot be empty"):
            await search_repository(
                object(),
                object(),
                INDEX_ID,
                query,
                embedding_provider_name="dashscope",
                embedding_model="test-model",
            )

    hybrid_mock.assert_not_awaited()


@pytest.mark.anyio
async def test_propagates_hybrid_search_failure() -> None:
    """Preserve downstream errors for the API or workflow boundary."""

    with patch(
        HYBRID_SEARCH_PATH,
        new_callable=AsyncMock,
    ) as hybrid_mock:
        hybrid_mock.side_effect = RuntimeError("Embedding service unavailable")

        with pytest.raises(
            RuntimeError,
            match="Embedding service unavailable",
        ):
            await search_repository(
                object(),
                object(),
                INDEX_ID,
                "哪里处理 CancelledError？",
                embedding_provider_name="dashscope",
                embedding_model="test-model",
            )

    hybrid_mock.assert_awaited_once()
