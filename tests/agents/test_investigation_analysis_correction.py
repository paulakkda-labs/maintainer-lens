"""One schema correction through real Graph execution, without provider I/O.

Simulated candidates verify protocol and boundaries, not model obedience.
"""

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from maintainer_lens.agents.investigation.state import InvalidInvestigationResultError
from maintainer_lens.agents.investigation_analysis import (
    AnalysisGenerationIncompleteError,
    create_llm_analyzer,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationInput
from maintainer_lens.services.investigation_execution import (
    InvestigationDeadlineExceeded,
    execute_graph_investigation,
)

pytestmark = pytest.mark.anyio
REPOSITORY = "correction-tests/repository"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _valid_analysis():
    return {
        "summary": "A previous discussion mentions a timeout; execution is unverified.",
        "findings": [
            {
                "statement": "The prior discussion reports a timeout.",
                "evidence_ids": ["history:item:124"],
            }
        ],
        "hypotheses": [],
        "unanswered_questions": ["Which version reproduces the timeout?"],
    }


def _invalid_content(kind="empty-citations"):
    value = _valid_analysis()
    if kind == "json":
        return '{"summary":'
    if kind == "empty-citations":
        value["findings"][0]["evidence_ids"] = []
    elif kind == "missing-field":
        del value["summary"]
    elif kind == "extra-field":
        value["unexpected"] = "PRIVATE_INVALID_FIELD_VALUE"
    else:
        raise AssertionError("Unknown invalid candidate")
    return json.dumps(value)


def _completion(content, *, finish_reason="stop"):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": finish_reason, "message": {"content": content}}
            ]
        },
    )


def _analyzer(client):
    return create_llm_analyzer(
        client,
        base_url="https://model.invalid/v1",
        api_key="test-key",
        model_name="user-configured-model",
        thinking_enabled=False,
        max_output_tokens=2048,
    )


def _history():
    return AsyncMock(
        return_value=(
            SimpleNamespace(
                repository=REPOSITORY,
                number=124,
                source_type="item",
                comment_id=None,
                title="Prior timeout discussion",
                content="A timeout was reported.",
                source_url=f"https://github.com/{REPOSITORY}/issues/124",
                source_updated_at=datetime(2026, 10, 30, tzinfo=UTC),
                distance=0.1,
            ),
        )
    )


async def _execute(client, *, language="en", timeout_seconds=5.0, history=None):
    return await execute_graph_investigation(
        InvestigationInput(
            repository=REPOSITORY,
            issue_number=123,
            issue_title="A timeout needs investigation",
            issue_body="The reporter observed a timeout.",
            response_language=language,
        ),
        history_searcher=history if history is not None else _history(),
        analyzer=_analyzer(client),
        timeout_seconds=timeout_seconds,
        max_search_rounds=1,
    )


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize(
    "kind", ["json", "empty-citations", "missing-field", "extra-field"]
)
async def test_one_correction_preserves_original_context_and_validates_new_analysis(
    language, kind
):
    captured = []
    invalid = _invalid_content(kind)
    expected = _valid_analysis()
    history = _history()

    def transport(request):
        captured.append(json.loads(request.content))
        assert len(captured) <= 2
        return _completion(invalid if len(captured) == 1 else json.dumps(expected))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await _execute(client, language=language, history=history)

    assert len(captured) == 2
    first, corrected = captured
    assert corrected["messages"][:2] == first["messages"]
    assert corrected["messages"][2] == {"role": "assistant", "content": invalid}
    correction = corrected["messages"][3]
    assert correction["role"] == "user"
    assert "Return one complete" in correction["content"]
    assert "Do not invent evidence IDs" in correction["content"]
    assert "untrusted data" in correction["content"]
    issues = json.loads(
        correction["content"].split("Validation issues (data):\n", 1)[1]
    )
    assert issues and all(set(issue) == {"type", "loc"} for issue in issues)
    assert "PRIVATE_INVALID_FIELD_VALUE" not in correction["content"]
    for payload in captured:
        assert payload["model"] == "user-configured-model"
        assert payload["max_tokens"] == 2048
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        assert (
            "in English" if language == "en" else "in Simplified Chinese"
        ) in payload["messages"][0]["content"]
    assert result.preliminary_analysis.model_dump() == expected
    assert result.response_language == language
    assert result.next_step == "needs_more_evidence"
    assert result.search_round == 1
    assert (
        result.analysis_evidence
        == json.loads(first["messages"][1]["content"])["evidence"]
    )
    history.assert_awaited_once()
    assert client.is_closed


async def test_second_invalid_candidate_fails_with_its_own_schema_error_without_third_call():
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        assert len(captured) <= 2
        return _completion(
            _invalid_content(
                "empty-citations" if len(captured) == 1 else "missing-field"
            )
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValidationError) as raised:
            await _execute(client)
    assert len(captured) == 2
    assert raised.value.errors()[0]["loc"] == ("summary",)


async def test_model_can_replace_uncited_observation_with_valid_empty_findings():
    calls = []
    expected = _valid_analysis()
    expected["findings"] = []

    def transport(request):
        calls.append(json.loads(request.content))
        assert len(calls) <= 2
        return _completion(
            _invalid_content() if len(calls) == 1 else json.dumps(expected)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await _execute(client)
    assert len(calls) == 2
    assert result.preliminary_analysis.model_dump() == expected
    assert result.analysis_evidence
    assert result.next_step == "needs_more_evidence"


@pytest.mark.parametrize("after_correction", [False, True])
async def test_well_formed_but_unsupplied_citations_fail_without_further_correction(
    after_correction,
):
    captured = []
    wrong = _valid_analysis()
    wrong["findings"][0]["evidence_ids"] = ["history:item:999"]

    def transport(request):
        captured.append(json.loads(request.content))
        return _completion(
            _invalid_content()
            if after_correction and len(captured) == 1
            else json.dumps(wrong)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(InvalidInvestigationResultError, match="not supplied"):
            await _execute(client)
    assert len(captured) == (2 if after_correction else 1)


@pytest.mark.parametrize("after_correction", [False, True])
@pytest.mark.parametrize(
    "failure", ["length", "content-filter", "http", "envelope", "empty-content"]
)
async def test_non_schema_failures_do_not_start_another_correction(
    failure, after_correction
):
    captured = []

    def transport(request):
        captured.append(json.loads(request.content))
        if after_correction and len(captured) == 1:
            return _completion(_invalid_content())
        if failure == "http":
            return httpx.Response(503)
        if failure == "envelope":
            return httpx.Response(200, json={"choices": []})
        if failure == "empty-content":
            return _completion(None)
        return _completion(
            json.dumps(_valid_analysis()),
            finish_reason="length" if failure == "length" else "content_filter",
        )

    expected = (
        httpx.HTTPStatusError
        if failure == "http"
        else AnalysisGenerationIncompleteError
        if failure in ("length", "content-filter")
        else ValueError
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(expected):
            await _execute(client)
    assert len(captured) == (2 if after_correction else 1)


@pytest.mark.parametrize(
    "failure", ["timeout", "http-timeout", "cancelled", "exception-group"]
)
async def test_correction_dependency_exceptions_keep_identity(failure):
    expected = {
        "timeout": TimeoutError("Upstream deadline"),
        "http-timeout": httpx.ReadTimeout("Provider read deadline"),
        "cancelled": asyncio.CancelledError("Upstream cancellation"),
        "exception-group": ExceptionGroup(
            "Provider failures", [RuntimeError("Unavailable")]
        ),
    }[failure]
    captured = []

    async def transport(request):
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return _completion(_invalid_content())
        raise expected

    # Direct analyzer use verifies dependency identity without Graph task wrappers.
    request = {
        "repository": REPOSITORY,
        "issue_number": 123,
        "issue_title": "Timeout",
        "issue_body": None,
        "response_language": "en",
        "evidence": [],
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(type(expected)) as raised:
            await _analyzer(client)(request)
    assert raised.value is expected
    assert len(captured) == 2
    assert client.is_closed


@pytest.mark.parametrize("swallow_cancellation", [False, True])
async def test_graph_deadline_covers_correction_and_waits_for_cleanup(
    swallow_cancellation,
):
    captured = []
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def transport(request):
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return _completion(_invalid_content())
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if swallow_cancellation:
                return _completion(json.dumps(_valid_analysis()))
            raise
        finally:
            cleaned.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(InvestigationDeadlineExceeded):
            await _execute(client, timeout_seconds=0.5)
    assert entered.is_set() and cleaned.is_set()
    assert len(captured) == 2
    assert client.is_closed


async def test_external_graph_cancellation_during_correction_waits_for_cleanup():
    captured = []
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def transport(request):
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return _completion(_invalid_content())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        task = asyncio.create_task(_execute(client))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel("External cancellation")
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set()
    assert len(captured) == 2
    assert client.is_closed


async def test_each_concurrent_analysis_has_its_own_single_correction_allowance():
    counts = Counter()

    async def transport(request):
        body = json.loads(request.content)
        number = json.loads(body["messages"][1]["content"])["issue_number"]
        counts[number] += 1
        assert counts[number] <= 2
        await asyncio.sleep(0)
        return _completion(
            _invalid_content() if counts[number] == 1 else json.dumps(_valid_analysis())
        )

    requests = [
        {
            "repository": REPOSITORY,
            "issue_number": number,
            "issue_title": "Timeout",
            "issue_body": None,
            "response_language": "en",
            "evidence": [
                {
                    "evidence_id": "history:item:124",
                    "category": "history",
                    "source_url": f"https://github.com/{REPOSITORY}/issues/124",
                    "content": "A timeout was reported.",
                }
            ],
        }
        for number in (122, 123)
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        analyzer = _analyzer(client)
        results = await asyncio.gather(*(analyzer(request) for request in requests))
    assert counts == {122: 2, 123: 2}
    assert all(result.model_dump() == _valid_analysis() for result in results)
