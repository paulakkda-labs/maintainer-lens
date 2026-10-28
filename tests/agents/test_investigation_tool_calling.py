"""Tests for one bounded LLM-selected search tool cycle."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from maintainer_lens.agents.investigation.tool_calling import (
    run_search_tool_step,
)

pytestmark = pytest.mark.anyio

INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
COMMIT_SHA = "a" * 40
QUERY = "CancelledError connection cleanup"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _history_hit() -> SimpleNamespace:
    return SimpleNamespace(
        repository="encode/httpx",
        number=3782,
        source_type="item",
        comment_id=None,
        title="Historical cancellation issue",
        content="Cancellation may affect active connections.",
        source_url="https://github.com/encode/httpx/issues/3782",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.12,
    )


def _source_hit() -> SimpleNamespace:
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
        cosine_distance=0.12,
        matched_in="content",
    )


def _tool_response(
    name: str,
    arguments: str,
    *,
    call_id: str = "call_123",
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
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            }
        ]
    }


def _final_response() -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "The search returned relevant evidence.",
                },
            }
        ]
    }


@pytest.mark.parametrize(
    "tool_name",
    ["search_history", "search_source"],
)
async def test_model_selects_and_executes_allowed_tool(
    tool_name: str,
) -> None:
    requests: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ("https://api.deepseek.com/chat/completions")

        requests.append(json.loads(request.content))

        if len(requests) == 1:
            return httpx.Response(
                200,
                json=_tool_response(
                    tool_name,
                    json.dumps({"query": QUERY}),
                ),
            )

        return httpx.Response(200, json=_final_response())

    history_searcher = AsyncMock(return_value=(_history_hit(),))
    source_searcher = AsyncMock(return_value=[_source_hit()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await run_search_tool_step(
            client,
            base_url="https://api.deepseek.com",
            api_key="test-api-key",
            model_name="deepseek-flash",
            repository="encode/httpx",
            question="Where is cancellation cleanup handled?",
            response_language="en",
            history_searcher=history_searcher,
            source_searcher=source_searcher,
            source_index_id=INDEX_ID,
        )

    assert len(requests) == 2
    assert result["tool_name"] == tool_name
    assert result["tool_call_id"] == "call_123"
    assert result["query"] == QUERY
    assert len(result["evidence"]) == 1
    assert result["model_response"] == ("The search returned relevant evidence.")

    first_request = requests[0]
    second_request = requests[1]

    assert first_request["tool_choice"] == "required"
    assert first_request["thinking"] == {"type": "disabled"}
    assert {tool["function"]["name"] for tool in first_request["tools"]} == {
        "search_history",
        "search_source",
    }

    assert second_request["tool_choice"] == "none"
    assert second_request["messages"][-2]["role"] == "assistant"

    tool_message = second_request["messages"][-1]

    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_123"

    returned_evidence = json.loads(tool_message["content"])["evidence"]

    assert returned_evidence == result["evidence"]

    if tool_name == "search_history":
        history_searcher.assert_awaited_once_with(
            "encode/httpx",
            QUERY,
        )
        source_searcher.assert_not_awaited()
        assert result["evidence"][0]["evidence_id"] == ("history:item:3782")
    else:
        source_searcher.assert_awaited_once_with(
            "encode/httpx",
            INDEX_ID,
            QUERY,
        )
        history_searcher.assert_not_awaited()
        assert result["evidence"][0]["evidence_id"] == (f"source:{INDEX_ID}:chunk-001")


async def test_unauthorized_tool_is_rejected_before_execution() -> None:
    requests: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_tool_response(
                "delete_repository",
                json.dumps({"query": QUERY}),
            ),
        )

    history_searcher = AsyncMock()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(
            ValueError,
            match="unauthorized tool",
        ):
            await run_search_tool_step(
                client,
                base_url="https://api.deepseek.com",
                api_key="test-api-key",
                model_name="deepseek-flash",
                repository="encode/httpx",
                question="Investigate cancellation",
                response_language="en",
                history_searcher=history_searcher,
            )

    assert len(requests) == 1
    history_searcher.assert_not_awaited()


@pytest.mark.parametrize(
    "arguments_json",
    [
        '{"query": "cleanup", "repository": "another/repo"}',
        '{"query": 42}',
        '{"query": "   "}',
        '{"query": ',
    ],
)
async def test_invalid_tool_arguments_are_rejected(
    arguments_json: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_tool_response(
                "search_history",
                arguments_json,
            ),
        )

    history_searcher = AsyncMock()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValidationError):
            await run_search_tool_step(
                client,
                base_url="https://api.deepseek.com",
                api_key="test-api-key",
                model_name="deepseek-flash",
                repository="encode/httpx",
                question="Investigate cancellation",
                response_language="en",
                history_searcher=history_searcher,
            )

    history_searcher.assert_not_awaited()


async def test_multiple_tool_calls_are_rejected() -> None:
    response_body = _tool_response(
        "search_history",
        json.dumps({"query": QUERY}),
    )
    tool_calls = response_body["choices"][0]["message"]["tool_calls"]

    tool_calls.append(
        {
            "id": "call_456",
            "type": "function",
            "function": {
                "name": "search_history",
                "arguments": json.dumps({"query": "pool release"}),
            },
        }
    )

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    history_searcher = AsyncMock()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(
            ValueError,
            match="exactly one tool",
        ):
            await run_search_tool_step(
                client,
                base_url="https://api.deepseek.com",
                api_key="test-api-key",
                model_name="deepseek-flash",
                repository="encode/httpx",
                question="Investigate cancellation",
                response_language="en",
                history_searcher=history_searcher,
            )

    history_searcher.assert_not_awaited()


async def test_search_failure_is_propagated() -> None:
    requests: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_tool_response(
                "search_history",
                json.dumps({"query": QUERY}),
            ),
        )

    history_searcher = AsyncMock(side_effect=RuntimeError("History RAG unavailable"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(
            RuntimeError,
            match="History RAG unavailable",
        ):
            await run_search_tool_step(
                client,
                base_url="https://api.deepseek.com",
                api_key="test-api-key",
                model_name="deepseek-flash",
                repository="encode/httpx",
                question="Investigate cancellation",
                response_language="en",
                history_searcher=history_searcher,
            )

    assert len(requests) == 1
    history_searcher.assert_awaited_once()


async def test_source_tool_requires_trusted_index_id() -> None:
    requests: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_final_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(
            ValueError,
            match="requires source_index_id",
        ):
            await run_search_tool_step(
                client,
                base_url="https://api.deepseek.com",
                api_key="test-api-key",
                model_name="deepseek-flash",
                repository="encode/httpx",
                question="Find cleanup implementation",
                response_language="en",
                history_searcher=AsyncMock(),
                source_searcher=AsyncMock(),
            )

    assert requests == []
