"""Tests for bounded follow-up searches in the investigation graph."""

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
FOLLOW_UP_QUERY = "How does the pool release cancelled connections?"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _issue_input(*, with_source: bool = False) -> dict:
    state = {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "Connection cancellation",
        "issue_body": "Connections may remain active after cancellation.",
        "response_language": "en",
    }

    if with_source:
        state["source_index_id"] = str(SOURCE_INDEX_ID)

    return state


def _history_hit(number: int) -> SimpleNamespace:
    return SimpleNamespace(
        repository="encode/httpx",
        number=number,
        source_type="item",
        comment_id=None,
        title=f"Historical issue #{number}",
        content=f"Discussion about connection handling in issue {number}.",
        source_url=f"https://github.com/encode/httpx/issues/{number}",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.12,
    )


def _source_hit(chunk_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        index_id=SOURCE_INDEX_ID,
        chunk_key=chunk_key,
        repository="encode/httpx",
        commit_sha=COMMIT_SHA,
        blob_sha="b" * 40,
        file_path="httpx/_client.py",
        language="python",
        kind="code",
        symbol="send",
        start_line=100,
        end_line=120,
        content=f"async def send(self, request): # {chunk_key}",
        rrf_score=0.03,
        vector_rank=1,
        keyword_rank=1,
        cosine_distance=0.12,
        matched_in="content",
    )


def _analysis(
    *,
    evidence_id: str = "history:item:3782",
    questions: list[str] | None = None,
    hypotheses: list[str] | None = None,
) -> InvestigationAnalysis:
    return InvestigationAnalysis(
        summary="Preliminary investigation completed.",
        findings=[
            AnalysisFinding(
                statement="A relevant discussion was retrieved.",
                evidence_ids=[evidence_id],
            )
        ],
        hypotheses=hypotheses if hypotheses is not None else [],
        unanswered_questions=questions if questions is not None else [],
    )


async def test_follow_up_retrieves_new_evidence_and_finishes() -> None:
    history_searcher = AsyncMock(
        side_effect=[
            (_history_hit(3782),),
            (_history_hit(1171),),
        ]
    )

    analyzer = AsyncMock(
        side_effect=[
            _analysis(questions=[FOLLOW_UP_QUERY]),
            _analysis(evidence_id="history:item:1171"),
        ]
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input())

    assert history_searcher.await_count == 2
    assert analyzer.await_count == 2

    assert result["search_round"] == 2
    assert len(result["searched_queries"]) == 2
    assert result["searched_queries"][1] == FOLLOW_UP_QUERY
    assert result["history_query"] == FOLLOW_UP_QUERY

    assert [hit["number"] for hit in result["history_evidence"]] == [
        1171,
        3782,
    ]

    assert result["new_history_evidence"] == 1
    assert result["next_step"] == "draft_preliminary_report"

    second_analysis_request = analyzer.await_args_list[1].args[0]

    assert [hit["evidence_id"] for hit in second_analysis_request["evidence"]] == [
        "history:item:1171",
        "history:item:3782",
    ]

    assert history_searcher.await_args_list[1].args == (
        "encode/httpx",
        FOLLOW_UP_QUERY,
    )


async def test_follow_up_with_only_old_evidence_skips_second_llm() -> None:
    history_searcher = AsyncMock(
        side_effect=[
            (_history_hit(3782),),
            (_history_hit(3782),),
        ]
    )

    analyzer = AsyncMock(
        return_value=_analysis(
            questions=[FOLLOW_UP_QUERY],
        )
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input())

    assert result["search_round"] == 2
    assert history_searcher.await_count == 2
    assert analyzer.await_count == 1

    assert len(result["history_evidence"]) == 1
    assert result["new_history_evidence"] == 0
    assert result["no_new_evidence"] is True

    assert result["next_step"] == "needs_more_evidence"
    assert result["routing_reason"] == ("The follow-up search found no new evidence.")


async def test_graph_stops_at_maximum_search_rounds() -> None:
    history_searcher = AsyncMock(
        side_effect=[
            (_history_hit(3782),),
            (_history_hit(1171),),
        ]
    )

    analyzer = AsyncMock(
        side_effect=[
            _analysis(questions=[FOLLOW_UP_QUERY]),
            _analysis(
                evidence_id="history:item:1171",
                questions=["Which additional connection paths are affected?"],
            ),
        ]
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input())

    assert history_searcher.await_count == 2
    assert analyzer.await_count == 2

    assert result["search_round"] == 2
    assert len(result["searched_queries"]) == 2
    assert result["next_step"] == "needs_more_evidence"
    assert "unanswered questions" in result["routing_reason"]


async def test_graph_does_not_repeat_the_initial_query() -> None:
    initial_query = (
        "Connection cancellation\nConnections may remain active after cancellation."
    )

    history_searcher = AsyncMock(return_value=(_history_hit(3782),))

    analyzer = AsyncMock(
        return_value=_analysis(
            questions=[initial_query],
        )
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input())

    history_searcher.assert_awaited_once()
    analyzer.assert_awaited_once()

    assert result["search_round"] == 1
    assert len(result["searched_queries"]) == 1
    assert result["next_step"] == "needs_more_evidence"


async def test_source_evidence_accumulates_without_duplicates() -> None:
    history_searcher = AsyncMock(
        side_effect=[
            (_history_hit(3782),),
            (_history_hit(3782),),
        ]
    )

    source_searcher = AsyncMock(
        side_effect=[
            [_source_hit("chunk-001")],
            [
                _source_hit("chunk-002"),
                _source_hit("chunk-001"),
            ],
        ]
    )

    analyzer = AsyncMock(
        side_effect=[
            _analysis(questions=[FOLLOW_UP_QUERY]),
            _analysis(evidence_id=f"source:{SOURCE_INDEX_ID}:chunk-002"),
        ]
    )

    graph = build_investigation_graph(
        history_searcher,
        source_searcher=source_searcher,
        analyzer=analyzer,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input(with_source=True))

    assert history_searcher.await_count == 2
    assert source_searcher.await_count == 2
    assert analyzer.await_count == 2

    assert result["new_history_evidence"] == 0
    assert result["new_source_evidence"] == 1

    assert [hit["chunk_key"] for hit in result["source_evidence"]] == [
        "chunk-002",
        "chunk-001",
    ]

    assert result["next_step"] == "draft_preliminary_report"

    second_request = analyzer.await_args_list[1].args[0]

    assert [
        hit["evidence_id"]
        for hit in second_request["evidence"]
        if hit["category"] == "source"
    ] == [
        f"source:{SOURCE_INDEX_ID}:chunk-002",
        f"source:{SOURCE_INDEX_ID}:chunk-001",
    ]


async def test_default_setting_preserves_single_round_behavior() -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(3782),))

    analyzer = AsyncMock(
        return_value=_analysis(
            questions=[FOLLOW_UP_QUERY],
        )
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
    )

    result = await graph.ainvoke(_issue_input())

    history_searcher.assert_awaited_once()
    analyzer.assert_awaited_once()

    assert result["search_round"] == 1
    assert result["next_step"] == "needs_more_evidence"


async def test_graph_does_not_retry_without_any_evidence() -> None:
    history_searcher = AsyncMock(return_value=())
    analyzer = AsyncMock()

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input())

    history_searcher.assert_awaited_once()
    analyzer.assert_not_awaited()

    assert result["search_round"] == 1
    assert result["history_evidence"] == []
    assert result["next_step"] == "needs_more_evidence"


@pytest.mark.parametrize("max_search_rounds", [0, 4, True, -1])
def test_graph_rejects_invalid_round_budget(
    max_search_rounds,
) -> None:
    history_searcher = AsyncMock()

    with pytest.raises(
        ValueError,
        match="max_search_rounds must be between 1 and 3",
    ):
        build_investigation_graph(
            history_searcher,
            max_search_rounds=max_search_rounds,
        )
