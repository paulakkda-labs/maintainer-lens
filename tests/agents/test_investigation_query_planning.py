"""Tests for LLM-guided follow-up query planning."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from maintainer_lens.agents.investigation.query_planning import (
    FollowUpQueryPlan,
    create_llm_query_planner,
)
from maintainer_lens.agents.investigation_analysis import (
    AnalysisFinding,
    InvestigationAnalysis,
)
from maintainer_lens.agents.investigation_graph import (
    build_investigation_graph,
)

pytestmark = pytest.mark.anyio

FOLLOW_UP_QUERY = "asyncio cancellation connection pool cleanup"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _issue_input() -> dict:
    return {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "Connection cancellation",
        "issue_body": "Connections remain active after cancellation.",
        "response_language": "zh",
    }


def _history_hit(number: int) -> SimpleNamespace:
    return SimpleNamespace(
        repository="encode/httpx",
        number=number,
        source_type="item",
        comment_id=None,
        title=f"Historical issue #{number}",
        content=f"Connection handling in issue {number}.",
        source_url=f"https://github.com/encode/httpx/issues/{number}",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.12,
    )


def _analysis(
    evidence_id: str,
    *,
    needs_more: bool,
) -> InvestigationAnalysis:
    return InvestigationAnalysis(
        summary="初步调查完成。",
        findings=[
            AnalysisFinding(
                statement="历史记录中存在相关讨论。",
                evidence_ids=[evidence_id],
            )
        ],
        hypotheses=[],
        unanswered_questions=(
            ["How does cancellation affect connection cleanup?"] if needs_more else []
        ),
    )


async def test_graph_uses_planned_query_for_second_round() -> None:
    history_searcher = AsyncMock(
        side_effect=[
            (_history_hit(3782),),
            (_history_hit(1171),),
        ]
    )

    analyzer = AsyncMock(
        side_effect=[
            _analysis("history:item:3782", needs_more=True),
            _analysis("history:item:1171", needs_more=False),
        ]
    )

    query_planner = AsyncMock(
        return_value=FollowUpQueryPlan(
            query=FOLLOW_UP_QUERY,
            reason="进一步搜索连接取消与清理逻辑。",
        )
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        query_planner=query_planner,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input())

    assert history_searcher.await_count == 2
    assert analyzer.await_count == 2
    query_planner.assert_awaited_once()

    assert result["search_round"] == 2
    assert result["history_query"] == FOLLOW_UP_QUERY
    assert result["searched_queries"][1] == FOLLOW_UP_QUERY
    assert result["next_step"] == "draft_preliminary_report"

    assert [hit["number"] for hit in result["history_evidence"]] == [1171, 3782]

    planning_request = query_planner.await_args.args[0]

    assert planning_request["response_language"] == "zh"
    assert planning_request["repository"] == "encode/httpx"
    assert planning_request["unanswered_questions"] == [
        "How does cancellation affect connection cleanup?"
    ]

    assert history_searcher.await_args_list[1].args == (
        "encode/httpx",
        FOLLOW_UP_QUERY,
    )


async def test_graph_rejects_duplicate_planned_query() -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(3782),))
    analyzer = AsyncMock(
        return_value=_analysis(
            "history:item:3782",
            needs_more=True,
        )
    )

    query_planner = AsyncMock(
        return_value=FollowUpQueryPlan(
            query=(
                " CONNECTION   CANCELLATION "
                "Connections remain active after cancellation. "
            ),
            reason="Try the initial query again.",
        )
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        query_planner=query_planner,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input())

    history_searcher.assert_awaited_once()
    analyzer.assert_awaited_once()
    query_planner.assert_awaited_once()

    assert result["search_round"] == 1
    assert result["next_step"] == "needs_more_evidence"
    assert result["planned_query"] is None
    assert result["routing_reason"] == (
        "Query planner returned a previously used query."
    )


async def test_graph_stops_when_planner_returns_no_query() -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(3782),))
    analyzer = AsyncMock(
        return_value=_analysis(
            "history:item:3782",
            needs_more=True,
        )
    )

    query_planner = AsyncMock(
        return_value=FollowUpQueryPlan(
            query=None,
            reason="没有找到有价值的新检索方向。",
        )
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        query_planner=query_planner,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input())

    history_searcher.assert_awaited_once()
    query_planner.assert_awaited_once()

    assert result["search_round"] == 1
    assert result["next_step"] == "needs_more_evidence"
    assert result["routing_reason"] == ("没有找到有价值的新检索方向。")


async def test_graph_skips_planner_when_analysis_is_ready() -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(3782),))
    analyzer = AsyncMock(
        return_value=_analysis(
            "history:item:3782",
            needs_more=False,
        )
    )
    query_planner = AsyncMock()

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        query_planner=query_planner,
        max_search_rounds=2,
    )

    result = await graph.ainvoke(_issue_input())

    query_planner.assert_not_awaited()
    history_searcher.assert_awaited_once()

    assert result["search_round"] == 1
    assert result["next_step"] == "draft_preliminary_report"


async def test_graph_propagates_planner_failure() -> None:
    history_searcher = AsyncMock(return_value=(_history_hit(3782),))
    analyzer = AsyncMock(
        return_value=_analysis(
            "history:item:3782",
            needs_more=True,
        )
    )
    query_planner = AsyncMock(side_effect=RuntimeError("Query planner unavailable"))

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        query_planner=query_planner,
        max_search_rounds=2,
    )

    with pytest.raises(
        RuntimeError,
        match="Query planner unavailable",
    ):
        await graph.ainvoke(_issue_input())

    history_searcher.assert_awaited_once()
    query_planner.assert_awaited_once()


def test_graph_rejects_unusable_planner_configuration() -> None:
    history_searcher = AsyncMock()
    query_planner = AsyncMock()
    analyzer = AsyncMock()

    with pytest.raises(ValueError, match="requires an analyzer"):
        build_investigation_graph(
            history_searcher,
            query_planner=query_planner,
            max_search_rounds=2,
        )

    with pytest.raises(
        ValueError,
        match="max_search_rounds greater than 1",
    ):
        build_investigation_graph(
            history_searcher,
            analyzer=analyzer,
            query_planner=query_planner,
        )


async def test_llm_query_planner_sends_structured_request() -> None:
    captured_requests: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == ("https://api.deepseek.com/chat/completions")

        captured_requests.append(json.loads(request.content))

        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "query": FOLLOW_UP_QUERY,
                                    "reason": "Search cancellation cleanup.",
                                }
                            )
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
    ) as client:
        planner = create_llm_query_planner(
            client,
            base_url="https://api.deepseek.com",
            api_key="test-api-key",
            model_name="deepseek-flash",
            thinking_enabled=False,
        )

        result = await planner(
            {
                "repository": "encode/httpx",
                "issue_title": "Connection cancellation",
                "issue_body": "Connection cleanup is incomplete.",
                "current_query": "connection cancellation",
                "searched_queries": ["connection cancellation"],
                "summary": "Root cause is not confirmed.",
                "unanswered_questions": ["How does cancellation affect cleanup?"],
                "hypotheses": ["Cleanup may be interrupted."],
                "response_language": "en",
            }
        )

    assert result.query == FOLLOW_UP_QUERY

    assert len(captured_requests) == 1
    payload = captured_requests[0]

    assert payload["model"] == "deepseek-flash"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}

    user_data = json.loads(payload["messages"][1]["content"])

    assert user_data["searched_queries"] == ["connection cancellation"]
    assert user_data["unanswered_questions"] == [
        "How does cancellation affect cleanup?"
    ]


async def test_llm_query_planner_rejects_invalid_response() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"query": FOLLOW_UP_QUERY})},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
    ) as client:
        planner = create_llm_query_planner(
            client,
            base_url="https://api.deepseek.com",
            api_key="test-api-key",
            model_name="deepseek-flash",
            thinking_enabled=False,
        )

        with pytest.raises(ValidationError):
            await planner(
                {
                    "repository": "encode/httpx",
                    "issue_title": "Cancellation",
                    "issue_body": None,
                    "current_query": "cancellation",
                    "searched_queries": ["cancellation"],
                    "summary": "Incomplete analysis.",
                    "unanswered_questions": ["What cleans up?"],
                    "hypotheses": [],
                    "response_language": "en",
                }
            )
