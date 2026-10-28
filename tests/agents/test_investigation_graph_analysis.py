"""Tests for LLM analysis in the investigation graph."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from maintainer_lens.agents.investigation_analysis import (
    AnalysisFinding,
    InvestigationAnalysis,
)
from maintainer_lens.agents.investigation_graph import (
    build_investigation_graph,
)

pytestmark = pytest.mark.anyio

INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
COMMIT_SHA = "a" * 40


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _issue_input() -> dict:
    return {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "Connection cancellation",
        "issue_body": "Connections remain active after cancellation.",
        "source_index_id": str(INDEX_ID),
    }


def _history_hit():
    return SimpleNamespace(
        repository="encode/httpx",
        number=3782,
        source_type="item",
        comment_id=None,
        title="Historical cancellation issue",
        content="Repeated cancellation may affect connections.",
        source_url="https://github.com/encode/httpx/issues/3782",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.1,
    )


def _source_hit():
    return SimpleNamespace(
        index_id=INDEX_ID,
        chunk_key="chunk-001",
        repository="encode/httpx",
        commit_sha=COMMIT_SHA,
        blob_sha="b" * 40,
        file_path="httpx/_client.py",
        language="python",
        kind="code",
        symbol="send",
        start_line=100,
        end_line=120,
        content="async def send(self, request): ...",
        rrf_score=0.03,
        vector_rank=1,
        keyword_rank=1,
        cosine_distance=0.1,
        matched_in="content",
    )


async def test_graph_analyzes_source_and_history_evidence() -> None:
    calls: list[str] = []

    async def history_searcher(repository: str, query: str):
        calls.append("history")
        return (_history_hit(),)

    async def source_searcher(
        repository: str,
        index_id: UUID,
        query: str,
    ):
        calls.append("source")
        return [_source_hit()]

    analysis = InvestigationAnalysis(
        summary="Cancellation behavior needs further investigation.",
        findings=[
            AnalysisFinding(
                statement="Historical discussions mention cancellation.",
                evidence_ids=["history:item:3782"],
            ),
            AnalysisFinding(
                statement="A relevant client method was retrieved.",
                evidence_ids=[f"source:{INDEX_ID}:chunk-001"],
            ),
        ],
        hypotheses=["Some cancellation paths may skip connection cleanup."],
        unanswered_questions=[
            "Does this code path release connections on cancellation?"
        ],
    )

    async def analyze(request):
        calls.append("analyze")
        assert request["repository"] == "encode/httpx"
        assert request["issue_number"] == 9000

        evidence_ids = {item["evidence_id"] for item in request["evidence"]}

        assert evidence_ids == {
            "history:item:3782",
            f"source:{INDEX_ID}:chunk-001",
        }

        return analysis

    graph = build_investigation_graph(
        history_searcher,
        source_searcher=source_searcher,
        analyzer=analyze,
    )

    result = await graph.ainvoke(_issue_input())

    assert calls == ["history", "source", "analyze"]

    assert result["preliminary_analysis"] == {
        "summary": ("Cancellation behavior needs further investigation."),
        "findings": [
            {
                "statement": ("Historical discussions mention cancellation."),
                "evidence_ids": ["history:item:3782"],
            },
            {
                "statement": "A relevant client method was retrieved.",
                "evidence_ids": [f"source:{INDEX_ID}:chunk-001"],
            },
        ],
        "hypotheses": ["Some cancellation paths may skip connection cleanup."],
        "unanswered_questions": [
            "Does this code path release connections on cancellation?"
        ],
    }


async def test_graph_rejects_fabricated_evidence_id() -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(),))

    analyzer = AsyncMock(
        return_value=InvestigationAnalysis(
            summary="A suspected issue was identified.",
            findings=[
                AnalysisFinding(
                    statement="An unsupported finding.",
                    evidence_ids=["history:item:999999"],
                )
            ],
            hypotheses=[],
            unanswered_questions=[],
        )
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
    )

    state = _issue_input()
    state.pop("source_index_id")

    with pytest.raises(
        ValueError,
        match="not supplied to this analysis",
    ):
        await graph.ainvoke(state)

    analyzer.assert_awaited_once()


async def test_graph_skips_llm_when_no_evidence() -> None:
    history_searcher = AsyncMock(return_value=())
    analyzer = AsyncMock()

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
    )

    state = _issue_input()
    state.pop("source_index_id")

    result = await graph.ainvoke(state)

    analyzer.assert_not_awaited()
    assert result["preliminary_analysis"]["findings"] == []
    assert result["preliminary_analysis"]["hypotheses"] == []


async def test_graph_propagates_llm_failure() -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(),))
    analyzer = AsyncMock(side_effect=RuntimeError("LLM provider unavailable"))

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
    )

    state = _issue_input()
    state.pop("source_index_id")

    with pytest.raises(
        RuntimeError,
        match="LLM provider unavailable",
    ):
        await graph.ainvoke(state)

    analyzer.assert_awaited_once()
