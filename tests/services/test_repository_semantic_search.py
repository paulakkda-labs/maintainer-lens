"""Tests for multilingual repository semantic search."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from maintainer_lens.services.repository_semantic_search import (
    search_repository_by_text,
)
from maintainer_lens.services.repository_vector_search import (
    RepositoryIndexNotFound,
    RepositoryIndexNotReady,
)


def make_session(
    *,
    status: str = "ready",
    provider_name: str = "dashscope",
    model: str = "test-model",
    exists: bool = True,
) -> MagicMock:
    """Create a mocked session containing repository index metadata."""

    session = MagicMock()

    index = SimpleNamespace(
        status=status,
        embedding_provider=provider_name,
        embedding_model=model,
    )

    session.get = AsyncMock(return_value=index if exists else None)

    return session


async def execute_search(
    session: MagicMock,
    provider: AsyncMock,
    query: str,
    *,
    top_k: int = 5,
) -> list:
    """Run a query with a consistent embedding configuration."""

    return await search_repository_by_text(
        session,
        provider,
        uuid4(),
        query,
        embedding_provider_name="dashscope",
        embedding_model="test-model",
        top_k=top_k,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "query",
    [
        "如何处理异步请求取消？",
        "Where is async request cancellation handled?",
        "如何处理 asyncio.CancelledError?",
    ],
)
async def test_multilingual_queries_use_same_search_pipeline(
    query: str,
) -> None:
    """Send Chinese, English, and mixed-language queries to one pipeline."""

    session = make_session()
    provider = AsyncMock()
    provider.embed_query.return_value = [0.1, 0.2, 0.3]

    expected_results = [SimpleNamespace(file_path="src/client.py")]

    with patch(
        "maintainer_lens.services.repository_semantic_search.search_repository_chunks",
        new_callable=AsyncMock,
        return_value=expected_results,
    ) as search_mock:
        results = await execute_search(session, provider, query)

    assert results == expected_results

    provider.embed_query.assert_awaited_once_with(query)

    search_mock.assert_awaited_once()

    assert search_mock.await_args.args[2] == [0.1, 0.2, 0.3]
    assert search_mock.await_args.kwargs["top_k"] == 5


@pytest.mark.anyio
async def test_query_is_trimmed_before_embedding() -> None:
    """Remove surrounding whitespace without translating the query."""

    session = make_session()
    provider = AsyncMock()
    provider.embed_query.return_value = [1.0, 0.0, 0.0]

    with patch(
        "maintainer_lens.services.repository_semantic_search.search_repository_chunks",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await execute_search(
            session,
            provider,
            "  如何处理请求取消？  ",
        )

    provider.embed_query.assert_awaited_once_with("如何处理请求取消？")


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
async def test_blank_query_is_rejected_before_database_access(
    query: str,
) -> None:
    """Reject empty input without calling PostgreSQL or Embedding."""

    session = make_session()
    provider = AsyncMock()

    with pytest.raises(ValueError, match="cannot be empty"):
        await execute_search(session, provider, query)

    session.get.assert_not_awaited()
    provider.embed_query.assert_not_awaited()


@pytest.mark.anyio
async def test_invalid_top_k_does_not_call_embedding() -> None:
    """Validate the result limit before paid requests."""

    session = make_session()
    provider = AsyncMock()

    with pytest.raises(ValueError, match="top_k"):
        await execute_search(session, provider, "查询", top_k=0)

    session.get.assert_not_awaited()
    provider.embed_query.assert_not_awaited()


@pytest.mark.anyio
async def test_missing_index_does_not_call_embedding() -> None:
    """Do not generate a query embedding for a missing index."""

    session = make_session(exists=False)
    provider = AsyncMock()

    with pytest.raises(RepositoryIndexNotFound):
        await execute_search(session, provider, "查找异常处理")

    provider.embed_query.assert_not_awaited()


@pytest.mark.anyio
async def test_unready_index_does_not_call_embedding() -> None:
    """Reject unfinished indexes before paid embedding calls."""

    session = make_session(status="building")
    provider = AsyncMock()

    with pytest.raises(RepositoryIndexNotReady):
        await execute_search(session, provider, "查找请求处理")

    provider.embed_query.assert_not_awaited()


@pytest.mark.anyio
async def test_model_mismatch_does_not_call_embedding() -> None:
    """Never mix embeddings produced by different named models."""

    session = make_session(model="another-model")
    provider = AsyncMock()

    with pytest.raises(ValueError, match="does not match"):
        await execute_search(session, provider, "查找请求处理")

    provider.embed_query.assert_not_awaited()


@pytest.mark.anyio
async def test_embedding_failure_does_not_search_database() -> None:
    """Propagate provider failures without executing vector retrieval."""

    session = make_session()
    provider = AsyncMock()

    provider.embed_query.side_effect = RuntimeError("Embedding API unavailable")

    with patch(
        "maintainer_lens.services.repository_semantic_search.search_repository_chunks",
        new_callable=AsyncMock,
    ) as search_mock:
        with pytest.raises(
            RuntimeError,
            match="Embedding API unavailable",
        ):
            await execute_search(
                session,
                provider,
                "如何处理请求取消？",
            )

    search_mock.assert_not_awaited()
