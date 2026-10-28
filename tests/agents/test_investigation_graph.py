"""Tests for the initial LangGraph investigation workflow."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

import maintainer_lens.agents.investigation.retrieval as workflow_module
from maintainer_lens.agents.investigation_graph import (
    build_investigation_graph,
    create_history_searcher,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _history_hit() -> RepositoryHistorySemanticHit:
    return RepositoryHistorySemanticHit(
        repository="encode/httpx",
        number=3782,
        source_type="item",
        comment_id=None,
        title="Connection cancellation",
        content="A connection remains active after cancellation.",
        source_url="https://github.com/encode/httpx/issues/3782",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.12,
    )


def _issue_input() -> dict:
    return {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "Async HTTP cancellation",
        "issue_body": "Connections remain active after task cancellation.",
    }


async def test_graph_prepares_query_and_returns_history_evidence() -> None:
    hit = _history_hit()
    searcher = AsyncMock(return_value=(hit,))
    graph = build_investigation_graph(searcher)

    result = await graph.ainvoke(_issue_input())

    searcher.assert_awaited_once_with(
        "encode/httpx",
        ("Async HTTP cancellation\nConnections remain active after task cancellation."),
    )

    assert result["repository"] == "encode/httpx"
    assert result["issue_number"] == 9000
    assert result["history_query"] == (
        "Async HTTP cancellation\nConnections remain active after task cancellation."
    )

    assert result["history_evidence"] == [
        {
            "repository": "encode/httpx",
            "number": 3782,
            "source_type": "item",
            "comment_id": None,
            "title": "Connection cancellation",
            "content": "A connection remains active after cancellation.",
            "source_url": "https://github.com/encode/httpx/issues/3782",
            "source_updated_at": "2026-10-25T00:00:00+00:00",
            "distance": 0.12,
        }
    ]


async def test_graph_returns_empty_evidence_without_matches() -> None:
    searcher = AsyncMock(return_value=())
    graph = build_investigation_graph(searcher)

    result = await graph.ainvoke(
        {
            "repository": "encode/httpx",
            "issue_number": 10,
            "issue_title": "Unknown issue",
            "issue_body": None,
        }
    )

    searcher.assert_awaited_once_with(
        "encode/httpx",
        "Unknown issue",
    )
    assert result["history_evidence"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": " "},
        {"issue_number": 0},
        {"issue_number": True},
        {"issue_title": " "},
    ],
)
async def test_graph_rejects_invalid_input_before_search(
    changes: dict,
) -> None:
    searcher = AsyncMock()
    graph = build_investigation_graph(searcher)

    state = _issue_input()
    state.update(changes)

    with pytest.raises(ValueError):
        await graph.ainvoke(state)

    searcher.assert_not_awaited()


async def test_graph_propagates_history_search_failure() -> None:
    searcher = AsyncMock(side_effect=RuntimeError("History search unavailable"))
    graph = build_investigation_graph(searcher)

    with pytest.raises(RuntimeError, match="History search unavailable"):
        await graph.ainvoke(_issue_input())

    searcher.assert_awaited_once()


class FakeSession:
    """Track session context entry and exit without a database."""

    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True


async def test_history_searcher_binds_dependencies_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    session_factory = Mock(return_value=session)
    embedding_client = Mock()

    semantic_search = AsyncMock(return_value=(_history_hit(),))

    monkeypatch.setattr(
        workflow_module,
        "search_repository_history_semantic",
        semantic_search,
    )

    searcher = create_history_searcher(
        session_factory,
        embedding_client,
        embedding_provider_name="openai-compatible",
        embedding_model="test-model",
        embedding_dimensions=1024,
        top_k=5,
    )

    result = await searcher(
        "encode/httpx",
        "connection cancellation",
    )

    assert result == (_history_hit(),)

    semantic_search.assert_awaited_once_with(
        session,
        embedding_client,
        "encode/httpx",
        "connection cancellation",
        embedding_provider_name="openai-compatible",
        embedding_model="test-model",
        embedding_dimensions=1024,
        top_k=5,
    )

    assert session.entered is True
    assert session.exited is True
