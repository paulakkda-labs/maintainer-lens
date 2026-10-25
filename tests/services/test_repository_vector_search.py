"""Unit tests for repository vector search validation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from maintainer_lens.services.repository_vector_search import (
    RepositoryIndexNotFound,
    RepositoryIndexNotReady,
    search_repository_chunks,
)


def make_session(index: object | None = None) -> MagicMock:
    """Create a session with a mocked repository index lookup."""

    session = MagicMock()
    session.scalar = AsyncMock(return_value=index)
    session.execute = AsyncMock()
    return session


@pytest.mark.anyio
@pytest.mark.parametrize("top_k", [0, -1, 21])
async def test_rejects_invalid_top_k(top_k: int) -> None:
    """Reject invalid result limits before querying PostgreSQL."""

    session = make_session()

    with pytest.raises(ValueError, match="top_k"):
        await search_repository_chunks(
            session,
            uuid4(),
            [1.0, 0.0, 0.0],
            top_k=top_k,
        )

    session.scalar.assert_not_awaited()
    session.execute.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query_vector", "message"),
    [
        ([], "empty"),
        ([0.0, 0.0, 0.0], "zero"),
        ([float("nan"), 0.0, 0.0], "non-finite"),
        ([float("inf"), 0.0, 0.0], "non-finite"),
        ([float("-inf"), 0.0, 0.0], "non-finite"),
    ],
)
async def test_rejects_invalid_query_vectors(
    query_vector: list[float],
    message: str,
) -> None:
    """Reject invalid embedding vectors before database access."""

    session = make_session()

    with pytest.raises(ValueError, match=message):
        await search_repository_chunks(
            session,
            uuid4(),
            query_vector,
        )

    session.scalar.assert_not_awaited()
    session.execute.assert_not_awaited()


@pytest.mark.anyio
async def test_rejects_missing_repository_index() -> None:
    """Distinguish a missing index from an empty search result."""

    session = make_session(index=None)

    with pytest.raises(RepositoryIndexNotFound):
        await search_repository_chunks(
            session,
            uuid4(),
            [1.0, 0.0, 0.0],
        )

    session.scalar.assert_awaited_once()
    session.execute.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["building", "failed"])
async def test_rejects_index_that_is_not_ready(status: str) -> None:
    """Never retrieve chunks from an incomplete index."""

    index = SimpleNamespace(
        status=status,
        embedding_dimensions=3,
    )
    session = make_session(index)

    with pytest.raises(RepositoryIndexNotReady):
        await search_repository_chunks(
            session,
            uuid4(),
            [1.0, 0.0, 0.0],
        )

    session.execute.assert_not_awaited()


@pytest.mark.anyio
async def test_rejects_query_dimension_mismatch() -> None:
    """Reject vectors with dimensions different from the index."""

    index = SimpleNamespace(
        status="ready",
        embedding_dimensions=3,
    )
    session = make_session(index)

    with pytest.raises(ValueError, match="dimension mismatch"):
        await search_repository_chunks(
            session,
            uuid4(),
            [1.0, 0.0],
        )

    session.execute.assert_not_awaited()
