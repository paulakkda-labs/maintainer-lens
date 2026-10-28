"""Tests for the OpenAI-compatible investigation analyzer."""

import json

import httpx
import pytest
from pydantic import ValidationError

from maintainer_lens.agents.investigation_analysis import (
    InvestigationAnalysis,
    create_llm_analyzer,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _analysis_request() -> dict:
    return {
        "repository": "encode/httpx",
        "issue_number": 3782,
        "issue_title": "Connection cancellation",
        "issue_body": "Connections may remain active.",
        "response_language": "en",
        "evidence": [
            {
                "evidence_id": "history:item:3782",
                "category": "history",
                "source_url": ("https://github.com/encode/httpx/issues/3782"),
                "content": "The issue discusses repeated cancellation.",
            }
        ],
    }


def _valid_analysis() -> dict:
    return {
        "summary": "Cancellation cleanup requires investigation.",
        "findings": [
            {
                "statement": ("A historical issue discusses repeated cancellation."),
                "evidence_ids": ["history:item:3782"],
            }
        ],
        "hypotheses": ["A cancellation path may leave a connection active."],
        "unanswered_questions": [
            "Does the client always release cancelled connections?"
        ],
    }


async def test_analyzer_sends_request_and_parses_structured_output() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)

        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(_valid_analysis()),
                        },
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handle)

    async with httpx.AsyncClient(transport=transport) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://llm.example/v1",
            api_key="test-api-key",
            model_name="test-model",
        )

        result = await analyzer(_analysis_request())

    assert isinstance(result, InvestigationAnalysis)
    assert result.summary == ("Cancellation cleanup requires investigation.")
    assert result.findings[0].evidence_ids == ["history:item:3782"]

    assert len(requests) == 1

    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == ("https://llm.example/v1/chat/completions")
    assert request.headers["Authorization"] == "Bearer test-api-key"

    payload = json.loads(request.content)

    assert payload["model"] == "test-model"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["stream"] is False
    assert payload["max_tokens"] == 4096
    assert payload["messages"][0]["role"] == "system"

    user_data = json.loads(payload["messages"][1]["content"])
    assert user_data["repository"] == "encode/httpx"
    assert user_data["evidence"][0]["evidence_id"] == ("history:item:3782")


async def test_analyzer_rejects_invalid_analysis_schema() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Incomplete response",
                                    "findings": [],
                                }
                            )
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://llm.example/v1",
            api_key="test-api-key",
            model_name="test-model",
        )

        with pytest.raises(ValidationError):
            await analyzer(_analysis_request())


@pytest.mark.parametrize(
    "content",
    ["{}", json.dumps(_valid_analysis())],
    ids=["missing-fields", "otherwise-valid-json"],
)
async def test_analyzer_rejects_truncated_generation(content: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": content,
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://llm.example/v1",
            api_key="test-api-key",
            model_name="test-model",
        )

        with pytest.raises(
            ValueError,
            match="did not finish normally",
        ):
            await analyzer(_analysis_request())


async def test_analyzer_propagates_http_failure() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "Rate limited"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://llm.example/v1",
            api_key="test-api-key",
            model_name="test-model",
        )

        with pytest.raises(httpx.HTTPStatusError):
            await analyzer(_analysis_request())


@pytest.mark.parametrize(
    ("response_language", "expected_instruction"),
    [
        ("zh", "Simplified Chinese"),
        ("en", "in English"),
    ],
)
async def test_analyzer_passes_language_instruction(
    response_language: str,
    expected_instruction: str,
) -> None:
    captured_payloads: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(_valid_analysis())},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
    ) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://llm.example/v1",
            api_key="test-api-key",
            model_name="test-model",
        )

        request = _analysis_request()
        request["response_language"] = response_language

        await analyzer(request)

    assert len(captured_payloads) == 1

    system_prompt = captured_payloads[0]["messages"][0]["content"]
    assert expected_instruction in system_prompt


@pytest.mark.parametrize(
    ("thinking_enabled", "expected_type"),
    [
        (True, "enabled"),
        (False, "disabled"),
        (None, None),
    ],
)
async def test_analyzer_sends_thinking_configuration(
    thinking_enabled: bool | None,
    expected_type: str | None,
) -> None:
    captured_requests: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))

        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(_valid_analysis()),
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
    ) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://api.deepseek.com",
            api_key="test-api-key",
            model_name="deepseek-flash",
            thinking_enabled=thinking_enabled,
        )

        await analyzer(_analysis_request())

    assert len(captured_requests) == 1

    request_body = captured_requests[0]

    if expected_type is None:
        assert "thinking" not in request_body
    else:
        assert request_body["thinking"] == {
            "type": expected_type,
        }


async def test_analysis_preserves_full_evidence_context_and_output_budget() -> None:
    request = _analysis_request()
    request["response_language"] = "zh"
    request["evidence"] = [
        {
            "evidence_id": f"history:item:{3782 + number}",
            "category": "history",
            "source_url": f"https://github.com/encode/httpx/issues/{3782 + number}",
            "content": "Untrusted historical context. " * 50,
        }
        for number in range(5)
    ] + [
        {
            "evidence_id": f"source:12345678-1234-4234-8234-123456789abc:{number:064x}",
            "category": "source",
            "source_url": f"https://github.com/encode/httpx/blob/{'a' * 40}/client.py#L1-L2",
            "content": "Untrusted source context. " * 60,
        }
        for number in range(5)
    ]
    expected = {
        "summary": "已有相关线索，根因尚未确定。",
        "findings": [
            {
                "statement": "历史记录讨论了取消行为。",
                "evidence_ids": ["history:item:3782"],
            }
        ],
        "hypotheses": [],
        "unanswered_questions": ["实际触发了哪条取消路径？"],
    }
    calls = []

    def handle(outbound: httpx.Request) -> httpx.Response:
        payload = json.loads(outbound.content)
        calls.append(payload)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 4096
        assert (
            json.loads(payload["messages"][1]["content"])["evidence"]
            == request["evidence"]
        )
        assert (
            "Fit the complete JSON within the output budget"
            in payload["messages"][0]["content"]
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(expected, ensure_ascii=False)
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://llm.example/v1",
            api_key="test-key",
            model_name="test-model",
            thinking_enabled=False,
        )
        result = await analyzer(request)
    assert result.model_dump() == expected and len(calls) == 1


@pytest.mark.parametrize("max_output_tokens", [1024, 8192])
async def test_analyzer_forwards_caller_selected_output_budget(max_output_tokens):
    sent = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(_valid_analysis())},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://llm.example/v1",
            api_key="test-key",
            model_name="test-model",
            max_output_tokens=max_output_tokens,
        )
        result = await analyzer(_analysis_request())
    assert result.model_dump() == _valid_analysis()
    assert sent == [max_output_tokens]


@pytest.mark.parametrize(
    "max_output_tokens",
    [True, False, 0, -1, 4096.0, "4096", None, float("nan"), float("inf")],
)
def test_analyzer_rejects_invalid_output_budget_before_client_use(max_output_tokens):
    with pytest.raises(
        ValueError, match="max_output_tokens must be a positive integer"
    ):
        create_llm_analyzer(
            object(),
            base_url="https://llm.example/v1",
            api_key="test-key",
            model_name="test-model",
            max_output_tokens=max_output_tokens,
        )
