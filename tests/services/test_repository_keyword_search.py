"""Unit tests for literal repository keyword search."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from maintainer_lens.services.repository_keyword_search import (
    _make_like_pattern,
    search_repository_by_keyword,
)
from maintainer_lens.services.repository_vector_search import (
    RepositoryIndexNotFound,
    RepositoryIndexNotReady,
)


def make_session(
    *,
    exists: bool = True,
    status: str = "ready",
) -> MagicMock:
    """Create a mocked session with repository index metadata."""

    session = MagicMock()
    session.get = AsyncMock(
        return_value=(SimpleNamespace(status=status) if exists else None)
    )
    session.execute = AsyncMock()
    return session


def test_like_pattern_escapes_wildcards() -> None:
    """Treat user-supplied percent and underscore as literal text."""

    assert _make_like_pattern("user_id%") == r"%user\_id\%%"


@pytest.mark.anyio
@pytest.mark.parametrize("keyword", ["", " ", "\n\t"])
async def test_rejects_blank_keyword(keyword: str) -> None:
    """Reject blank queries before accessing the database."""

    session = make_session()

    with pytest.raises(ValueError, match="cannot be empty"):
        await search_repository_by_keyword(
            session,
            uuid4(),
            keyword,
        )

    session.get.assert_not_awaited()


@pytest.mark.anyio
async def test_rejects_oversized_keyword() -> None:
    """Bound the size of a literal keyword search."""

    session = make_session()

    with pytest.raises(ValueError, match="too long"):
        await search_repository_by_keyword(
            session,
            uuid4(),
            "a" * 129,
        )

    session.get.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("top_k", [0, -1, 21])
async def test_rejects_invalid_top_k(top_k: int) -> None:
    """Reject invalid result limits before database access."""

    session = make_session()

    with pytest.raises(ValueError, match="top_k"):
        await search_repository_by_keyword(
            session,
            uuid4(),
            "CancelledError",
            top_k=top_k,
        )

    session.get.assert_not_awaited()


@pytest.mark.anyio
async def test_rejects_missing_index() -> None:
    """Differentiate a missing index from no keyword matches."""

    session = make_session(exists=False)

    with pytest.raises(RepositoryIndexNotFound):
        await search_repository_by_keyword(
            session,
            uuid4(),
            "CancelledError",
        )

    session.execute.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["building", "failed"])
async def test_rejects_unready_index(status: str) -> None:
    """Prevent retrieval from an incomplete repository index."""

    session = make_session(status=status)

    with pytest.raises(RepositoryIndexNotReady):
        await search_repository_by_keyword(
            session,
            uuid4(),
            "CancelledError",
        )

    session.execute.assert_not_awaited()
