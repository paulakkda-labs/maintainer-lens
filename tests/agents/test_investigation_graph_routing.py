"""Tests for conditional routing in the investigation graph."""

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

SOURCE_INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
COMMIT_SHA = "a" * 40


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _issue_input(
    *,
    with_source: bool = False,
    language: str = "en",
) -> dict:
    state = {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "Connection cancellation",
        "issue_body": "Connections may remain active after cancellation.",
        "response_language": language,
    }

    if with_source:
        state["source_index_id"] = str(SOURCE_INDEX_ID)

    return state


def _history_hit() -> SimpleNamespace:
    return SimpleNamespace(
        repository="encode/httpx",
        number=3782,
        source_type="item",
        comment_id=None,
        title="Historical cancellation issue",
        content="Repeated cancellation may leave connections active.",
        source_url="https://github.com/encode/httpx/issues/3782",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.12,
    )


def _source_hit() -> SimpleNamespace:
    return SimpleNamespace(
        index_id=SOURCE_INDEX_ID,
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
        cosine_distance=0.12,
        matched_in="content",
    )


def _finding() -> AnalysisFinding:
    return AnalysisFinding(
        statement="A historical discussion mentions cancellation.",
        evidence_ids=["history:item:3782"],
    )


async def test_graph_routes_to_preliminary_report_when_ready() -> None:
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
        assert index_id == SOURCE_INDEX_ID
        return [_source_hit()]

    async def analyzer(request):
        calls.append("analyzer")

        assert request["response_language"] == "zh"

        evidence_ids = {evidence["evidence_id"] for evidence in request["evidence"]}

        assert evidence_ids == {
            "history:item:3782",
            f"source:{SOURCE_INDEX_ID}:chunk-001",
        }

        return InvestigationAnalysis(
            summary="现有证据支持初步分析。",
            findings=[_finding()],
            hypotheses=[],
            unanswered_questions=[],
        )

    graph = build_investigation_graph(
        history_searcher,
        source_searcher=source_searcher,
        analyzer=analyzer,
    )

    result = await graph.ainvoke(_issue_input(with_source=True, language="zh"))

    assert calls == ["history", "source", "analyzer"]

    assert result["next_step"] == "draft_preliminary_report"
    assert result["routing_reason"] == (
        "The preliminary analysis has cited findings and no declared gaps."
    )

    assert len(result["history_evidence"]) == 1
    assert len(result["source_evidence"]) == 1
    assert len(result["preliminary_analysis"]["findings"]) == 1


@pytest.mark.parametrize(
    ("has_finding", "hypotheses", "questions", "reason"),
    [
        (
            False,
            [],
            [],
            "no supported findings",
        ),
        (
            True,
            [],
            ["What happens during cleanup?"],
            "unanswered questions",
        ),
        (
            True,
            ["Cleanup may be interrupted."],
            [],
            "unverified hypotheses",
        ),
    ],
)
async def test_graph_routes_to_more_evidence_for_incomplete_analysis(
    has_finding: bool,
    hypotheses: list[str],
    questions: list[str],
    reason: str,
) -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(),))

    analyzer = AsyncMock(
        return_value=InvestigationAnalysis(
            summary="More investigation may be necessary.",
            findings=[_finding()] if has_finding else [],
            hypotheses=hypotheses,
            unanswered_questions=questions,
        )
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
    )

    result = await graph.ainvoke(_issue_input())

    assert result["next_step"] == "needs_more_evidence"
    assert reason in result["routing_reason"]

    history_searcher.assert_awaited_once()
    analyzer.assert_awaited_once()


async def test_graph_routes_to_more_evidence_without_search_results() -> None:
    history_searcher = AsyncMock(return_value=())
    analyzer = AsyncMock()

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
    )

    result = await graph.ainvoke(_issue_input())

    assert result["history_evidence"] == []
    assert result["preliminary_analysis"]["findings"] == []
    assert result["next_step"] == "needs_more_evidence"
    assert result["routing_reason"] == (
        "No source or historical evidence was retrieved."
    )

    history_searcher.assert_awaited_once()
    analyzer.assert_not_awaited()


async def test_graph_preserves_existing_retrieval_only_mode() -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(),))

    graph = build_investigation_graph(history_searcher)

    result = await graph.ainvoke(_issue_input())

    assert len(result["history_evidence"]) == 1
    assert "preliminary_analysis" not in result
    assert "next_step" not in result
    assert "routing_reason" not in result

    history_searcher.assert_awaited_once()


async def test_graph_propagates_analysis_failure_before_routing() -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(),))
    analyzer = AsyncMock(side_effect=RuntimeError("LLM analysis failed"))

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
    )

    with pytest.raises(
        RuntimeError,
        match="LLM analysis failed",
    ):
        await graph.ainvoke(_issue_input())

    history_searcher.assert_awaited_once()
    analyzer.assert_awaited_once()
