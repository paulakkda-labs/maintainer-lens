"""Tests for tool-calling integration with the investigation graph."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from maintainer_lens.agents.investigation.tool_node import (
    create_tool_search_runner,
)
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
TOOL_QUERY = "AsyncClient aclose CancelledError connection pool cleanup"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _issue_input() -> dict:
    return {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "Connection cancellation",
        "issue_body": "Connections may remain active after cancellation.",
        "source_index_id": str(INDEX_ID),
        "response_language": "zh",
    }


def _history_hit(number: int = 3782) -> SimpleNamespace:
    return SimpleNamespace(
        repository="encode/httpx",
        number=number,
        source_type="item",
        comment_id=None,
        title=f"Historical issue #{number}",
        content="Cancellation may affect active connections.",
        source_url=f"https://github.com/encode/httpx/issues/{number}",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.12,
    )


def _source_hit(chunk_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        index_id=INDEX_ID,
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
    evidence_id: str,
    *,
    needs_more: bool,
) -> InvestigationAnalysis:
    return InvestigationAnalysis(
        summary="初步调查完成。",
        findings=[
            AnalysisFinding(
                statement="找到了相关证据。",
                evidence_ids=[evidence_id],
            )
        ],
        hypotheses=[],
        unanswered_questions=(
            ["How does cancellation affect connection cleanup?"] if needs_more else []
        ),
    )


def _tool_selection(
    query: str,
    *,
    tool_name: str = "search_source",
) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_test_123",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps({"query": query}),
                            },
                        }
                    ],
                },
            }
        ]
    }


def _tool_feedback() -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "相关源码已检索。",
                },
            }
        ]
    }


def _make_transport(
    query: str,
    captured_requests: list[dict],
    *,
    tool_name: str = "search_source",
    feedback: dict | None = None,
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))

        if len(captured_requests) == 1:
            return httpx.Response(
                200,
                json=_tool_selection(query, tool_name=tool_name),
            )

        if len(captured_requests) == 2:
            return httpx.Response(
                200, json=feedback if feedback is not None else _tool_feedback()
            )

        raise AssertionError("Unexpected additional LLM request")

    return httpx.MockTransport(handle)


async def test_graph_uses_one_selected_tool_and_reanalyzes() -> None:
    requests: list[dict] = []

    history_searcher = AsyncMock(return_value=(_history_hit(),))
    source_searcher = AsyncMock(
        side_effect=[
            [_source_hit("chunk-001")],
            [_source_hit("chunk-002")],
        ]
    )
    analyzer = AsyncMock(
        side_effect=[
            _analysis("history:item:3782", needs_more=True),
            _analysis(
                f"source:{INDEX_ID}:chunk-002",
                needs_more=False,
            ),
        ]
    )

    async with httpx.AsyncClient(
        transport=_make_transport(TOOL_QUERY, requests),
    ) as client:
        runner = create_tool_search_runner(
            client,
            base_url="https://api.deepseek.com",
            api_key="test-api-key",
            model_name="deepseek-flash",
            history_searcher=history_searcher,
            source_searcher=source_searcher,
        )

        graph = build_investigation_graph(
            history_searcher,
            source_searcher=source_searcher,
            analyzer=analyzer,
            tool_search_runner=runner,
            max_search_rounds=2,
        )

        result = await graph.ainvoke(_issue_input())

    assert len(requests) == 2
    assert history_searcher.await_count == 1
    assert source_searcher.await_count == 2
    assert analyzer.await_count == 2

    assert result["search_round"] == 2
    assert result["history_query"] == TOOL_QUERY
    assert result["searched_queries"][1] == TOOL_QUERY

    assert [hit["chunk_key"] for hit in result["source_evidence"]] == [
        "chunk-002",
        "chunk-001",
    ]

    assert len(result["history_evidence"]) == 1
    assert result["new_history_evidence"] == 0
    assert result["new_source_evidence"] == 1
    assert result["next_step"] == "draft_preliminary_report"

    assert result["last_tool_name"] == "search_source"
    assert result["last_tool_call_id"] == "call_test_123"
    assert result["last_tool_feedback"] == "相关源码已检索。"

    source_searcher.assert_any_await(
        "encode/httpx",
        INDEX_ID,
        TOOL_QUERY,
    )

    second_analysis_request = analyzer.await_args_list[1].args[0]
    assert "相关源码已检索。" not in json.dumps(
        second_analysis_request, ensure_ascii=False
    )
    assert (
        "acknowledge receipt in one short sentence"
        in requests[1]["messages"][0]["content"]
    )

    evidence_ids = {
        evidence["evidence_id"] for evidence in second_analysis_request["evidence"]
    }

    assert f"source:{INDEX_ID}:chunk-002" in evidence_ids
    assert "history:item:3782" in evidence_ids

    assert requests[0]["tool_choice"] == "required"
    assert requests[1]["tool_choice"] == "none"

    tool_message = requests[1]["messages"][-1]

    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_test_123"


@pytest.mark.parametrize("failure", ["length", "unexpected-tool-call"])
async def test_graph_rejects_invalid_feedback_before_next_analysis_without_retry(
    failure,
):
    feedback = _tool_feedback()
    if failure == "length":
        feedback["choices"][0]["finish_reason"] = "length"
    else:
        feedback["choices"][0]["message"]["tool_calls"] = [{"id": "unapproved"}]
    requests = []
    history = AsyncMock(return_value=(_history_hit(),))
    source = AsyncMock(
        side_effect=[[_source_hit("chunk-001")], [_source_hit("chunk-002")]]
    )
    analyzer = AsyncMock(return_value=_analysis("history:item:3782", needs_more=True))
    async with httpx.AsyncClient(
        transport=_make_transport(TOOL_QUERY, requests, feedback=feedback)
    ) as client:
        runner = create_tool_search_runner(
            client,
            base_url="https://model.invalid/v1",
            api_key="test-key",
            model_name="configured-model",
            history_searcher=history,
            source_searcher=source,
        )
        graph = build_investigation_graph(
            history,
            source_searcher=source,
            analyzer=analyzer,
            tool_search_runner=runner,
            max_search_rounds=2,
        )
        with pytest.raises(ValueError, match="did not finish after the tool result"):
            await graph.ainvoke(_issue_input())
    assert len(requests) == 2
    assert source.await_count == 2
    history.assert_awaited_once()
    analyzer.assert_awaited_once()
    assert client.is_closed


async def test_duplicate_query_stops_before_second_retrieval() -> None:
    requests: list[dict] = []

    initial_query = (
        "CONNECTION   CANCELLATION Connections may remain active after cancellation."
    )

    history_searcher = AsyncMock(return_value=(_history_hit(),))
    source_searcher = AsyncMock(return_value=[_source_hit("chunk-001")])
    analyzer = AsyncMock(
        return_value=_analysis(
            "history:item:3782",
            needs_more=True,
        )
    )

    async with httpx.AsyncClient(
        transport=_make_transport(initial_query, requests),
    ) as client:
        runner = create_tool_search_runner(
            client,
            base_url="https://api.deepseek.com",
            api_key="test-api-key",
            model_name="deepseek-flash",
            history_searcher=history_searcher,
            source_searcher=source_searcher,
        )

        graph = build_investigation_graph(
            history_searcher,
            source_searcher=source_searcher,
            analyzer=analyzer,
            tool_search_runner=runner,
            max_search_rounds=2,
        )

        result = await graph.ainvoke(_issue_input())

    assert len(requests) == 1

    history_searcher.assert_awaited_once()
    source_searcher.assert_awaited_once()
    analyzer.assert_awaited_once()

    assert result["search_round"] == 1
    assert len(result["searched_queries"]) == 1
    assert result["tool_search_skipped"] is True
    assert result["next_step"] == "needs_more_evidence"
    assert "previously searched query" in result["routing_reason"]


async def test_no_new_tool_evidence_skips_second_analysis() -> None:
    requests: list[dict] = []

    history_searcher = AsyncMock(return_value=(_history_hit(),))
    source_searcher = AsyncMock(
        side_effect=[
            [_source_hit("chunk-001")],
            [_source_hit("chunk-001")],
        ]
    )
    analyzer = AsyncMock(
        return_value=_analysis(
            "history:item:3782",
            needs_more=True,
        )
    )

    async with httpx.AsyncClient(
        transport=_make_transport(TOOL_QUERY, requests),
    ) as client:
        runner = create_tool_search_runner(
            client,
            base_url="https://api.deepseek.com",
            api_key="test-api-key",
            model_name="deepseek-flash",
            history_searcher=history_searcher,
            source_searcher=source_searcher,
        )

        graph = build_investigation_graph(
            history_searcher,
            source_searcher=source_searcher,
            analyzer=analyzer,
            tool_search_runner=runner,
            max_search_rounds=2,
        )

        result = await graph.ainvoke(_issue_input())

    assert len(requests) == 2

    history_searcher.assert_awaited_once()
    assert source_searcher.await_count == 2
    analyzer.assert_awaited_once()

    assert result["search_round"] == 2
    assert result["no_new_evidence"] is True
    assert result["new_source_evidence"] == 0
    assert result["next_step"] == "needs_more_evidence"
    assert result["routing_reason"] == ("The follow-up search found no new evidence.")


def test_graph_rejects_invalid_tool_configuration() -> None:
    history_searcher = AsyncMock()
    analyzer = AsyncMock()
    tool_search_runner = AsyncMock()
    query_planner = AsyncMock()

    with pytest.raises(ValueError, match="requires an analyzer"):
        build_investigation_graph(
            history_searcher,
            tool_search_runner=tool_search_runner,
            max_search_rounds=2,
        )

    with pytest.raises(
        ValueError,
        match="max_search_rounds greater than 1",
    ):
        build_investigation_graph(
            history_searcher,
            analyzer=analyzer,
            tool_search_runner=tool_search_runner,
        )

    with pytest.raises(
        ValueError,
        match="Cannot enable query_planner",
    ):
        build_investigation_graph(
            history_searcher,
            analyzer=analyzer,
            query_planner=query_planner,
            tool_search_runner=tool_search_runner,
            max_search_rounds=2,
        )


async def test_history_tool_does_not_repeat_source_search() -> None:
    requests: list[dict] = []

    history_searcher = AsyncMock(
        side_effect=[
            (_history_hit(3782),),
            (_history_hit(1171),),
        ]
    )
    source_searcher = AsyncMock(return_value=[_source_hit("chunk-001")])
    analyzer = AsyncMock(
        side_effect=[
            _analysis("history:item:3782", needs_more=True),
            _analysis("history:item:1171", needs_more=False),
        ]
    )

    async with httpx.AsyncClient(
        transport=_make_transport(
            TOOL_QUERY,
            requests,
            tool_name="search_history",
        )
    ) as client:
        runner = create_tool_search_runner(
            client,
            base_url="https://api.deepseek.com",
            api_key="test-api-key",
            model_name="deepseek-flash",
            history_searcher=history_searcher,
            source_searcher=source_searcher,
        )

        graph = build_investigation_graph(
            history_searcher,
            source_searcher=source_searcher,
            analyzer=analyzer,
            tool_search_runner=runner,
            max_search_rounds=2,
        )

        result = await graph.ainvoke(_issue_input())

    assert history_searcher.await_count == 2
    source_searcher.assert_awaited_once()
    assert analyzer.await_count == 2

    assert [hit["number"] for hit in result["history_evidence"]] == [1171, 3782]

    assert result["new_history_evidence"] == 1
    assert result["new_source_evidence"] == 0
    assert result["last_tool_name"] == "search_history"
    assert result["next_step"] == "draft_preliminary_report"
