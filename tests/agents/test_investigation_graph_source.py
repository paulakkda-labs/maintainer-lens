"""Tests for source retrieval in the LangGraph investigation workflow."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

import maintainer_lens.agents.investigation.retrieval as workflow_module
from maintainer_lens.agents.investigation_graph import (
    build_investigation_graph,
    create_source_searcher,
)
from maintainer_lens.db.models.repository_index import RepositoryIndex

pytestmark = pytest.mark.anyio

SOURCE_INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
COMMIT_SHA = "a" * 40
BLOB_SHA = "b" * 40


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _issue_input() -> dict:
    return {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "AsyncClient connection cancellation",
        "issue_body": "Connections are not released after cancellation.",
        "source_index_id": str(SOURCE_INDEX_ID),
    }


def _source_hit(**changes):
    fields = {
        "index_id": SOURCE_INDEX_ID,
        "chunk_key": "chunk-001",
        "repository": "encode/httpx",
        "commit_sha": COMMIT_SHA,
        "blob_sha": BLOB_SHA,
        "file_path": "src/client request.py",
        "language": "python",
        "kind": "code",
        "symbol": "send",
        "start_line": 120,
        "end_line": 150,
        "content": "async def send(self, request): ...",
        "rrf_score": 0.032787,
        "vector_rank": 1,
        "keyword_rank": 1,
        "cosine_distance": 0.12,
        "matched_in": "content",
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


async def test_graph_retrieves_history_then_source() -> None:
    calls: list[str] = []

    async def search_history(repository: str, query: str):
        calls.append("history")
        assert repository == "encode/httpx"
        assert "AsyncClient connection cancellation" in query
        return ()

    async def search_source(repository: str, index_id: UUID, query: str):
        calls.append("source")
        assert repository == "encode/httpx"
        assert index_id == SOURCE_INDEX_ID
        assert "Connections are not released" in query
        return [_source_hit()]

    graph = build_investigation_graph(
        search_history,
        source_searcher=search_source,
    )

    result = await graph.ainvoke(_issue_input())

    assert calls == ["history", "source"]
    assert result["history_evidence"] == []
    assert result["source_index_id"] == str(SOURCE_INDEX_ID)

    assert result["source_evidence"] == [
        {
            "index_id": str(SOURCE_INDEX_ID),
            "chunk_key": "chunk-001",
            "repository": "encode/httpx",
            "commit_sha": COMMIT_SHA,
            "blob_sha": BLOB_SHA,
            "file_path": "src/client request.py",
            "language": "python",
            "kind": "code",
            "symbol": "send",
            "start_line": 120,
            "end_line": 150,
            "content": "async def send(self, request): ...",
            "source_url": (
                f"https://github.com/encode/httpx/blob/{COMMIT_SHA}/"
                "src/client%20request.py#L120-L150"
            ),
            "rrf_score": 0.032787,
            "vector_rank": 1,
            "keyword_rank": 1,
            "cosine_distance": 0.12,
            "matched_in": "content",
        }
    ]


@pytest.mark.parametrize(
    "invalid_id",
    [None, "", "not-a-uuid"],
)
async def test_graph_rejects_invalid_source_index_before_search(
    invalid_id,
) -> None:
    history_searcher = AsyncMock(return_value=())
    source_searcher = AsyncMock(return_value=[])

    graph = build_investigation_graph(
        history_searcher,
        source_searcher=source_searcher,
    )

    initial_state = _issue_input()

    if invalid_id is None:
        initial_state.pop("source_index_id")
    else:
        initial_state["source_index_id"] = invalid_id

    with pytest.raises(ValueError):
        await graph.ainvoke(initial_state)

    history_searcher.assert_not_awaited()
    source_searcher.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": "another/repository"},
        {"index_id": uuid4()},
        {"commit_sha": "invalid-sha"},
        {"start_line": 0},
        {"end_line": 100},
        {"file_path": "../private.py"},
    ],
)
async def test_graph_rejects_invalid_source_evidence(
    changes: dict,
) -> None:
    history_searcher = AsyncMock(return_value=())
    source_searcher = AsyncMock(return_value=[_source_hit(**changes)])

    graph = build_investigation_graph(
        history_searcher,
        source_searcher=source_searcher,
    )

    with pytest.raises(ValueError):
        await graph.ainvoke(_issue_input())

    history_searcher.assert_awaited_once()
    source_searcher.assert_awaited_once()


async def test_graph_propagates_source_search_failure() -> None:
    history_searcher = AsyncMock(return_value=())
    source_searcher = AsyncMock(
        side_effect=RuntimeError("Source retrieval unavailable")
    )

    graph = build_investigation_graph(
        history_searcher,
        source_searcher=source_searcher,
    )

    with pytest.raises(RuntimeError, match="Source retrieval unavailable"):
        await graph.ainvoke(_issue_input())

    history_searcher.assert_awaited_once()
    source_searcher.assert_awaited_once()


class FakeSession:
    """Track repository-index lookup and session cleanup."""

    def __init__(self, repository: str) -> None:
        self.get = AsyncMock(
            return_value=SimpleNamespace(
                id=SOURCE_INDEX_ID,
                repository=repository,
                commit_sha=COMMIT_SHA,
                tree_sha="d" * 40,
                embedding_provider="openai-compatible",
                embedding_model="test-model",
                embedding_dimensions=1024,
                chunker_version="test-chunker",
                status="ready",
            )
        )
        self.entered = False
        self.exited = False
        self.in_transaction = False

    @asynccontextmanager
    async def begin(self):
        assert not self.in_transaction
        self.in_transaction = True
        try:
            yield self
        finally:
            self.in_transaction = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True


async def test_source_searcher_delegates_to_existing_hybrid_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession("encode/httpx")
    session_factory = Mock(return_value=session)
    embedding_client = Mock()

    async def retrieve(*args, **kwargs):
        assert not session.in_transaction
        return [_source_hit()]

    search_repository = AsyncMock(side_effect=retrieve)

    monkeypatch.setattr(
        workflow_module,
        "search_repository",
        search_repository,
    )

    source_searcher = create_source_searcher(
        session_factory,
        embedding_client,
        embedding_provider_name="openai-compatible",
        embedding_model="test-model",
        embedding_dimensions=1024,
        candidate_limit=10,
        top_k=5,
        rank_constant=60,
    )

    hits = await source_searcher(
        "encode/httpx",
        SOURCE_INDEX_ID,
        "AsyncClient cancellation",
    )

    assert len(hits) == 1

    session.get.assert_awaited_once_with(
        RepositoryIndex,
        SOURCE_INDEX_ID,
        populate_existing=True,
    )

    search_repository.assert_awaited_once_with(
        session,
        embedding_client,
        SOURCE_INDEX_ID,
        "AsyncClient cancellation",
        embedding_provider_name="openai-compatible",
        embedding_model="test-model",
        candidate_limit=10,
        top_k=5,
        rank_constant=60,
    )

    assert session.entered is True
    assert session.exited is True


async def test_source_searcher_rejects_foreign_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession("another/repository")
    session_factory = Mock(return_value=session)
    embedding_client = Mock()

    search_repository = AsyncMock()

    monkeypatch.setattr(
        workflow_module,
        "search_repository",
        search_repository,
    )

    source_searcher = create_source_searcher(
        session_factory,
        embedding_client,
        embedding_provider_name="openai-compatible",
        embedding_model="test-model",
        embedding_dimensions=1024,
    )

    with pytest.raises(
        ValueError,
        match="does not belong to the requested repository",
    ):
        await source_searcher(
            "encode/httpx",
            SOURCE_INDEX_ID,
            "AsyncClient cancellation",
        )

    search_repository.assert_not_awaited()
    assert session.exited is True
