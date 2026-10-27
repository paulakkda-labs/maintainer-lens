"""Regression tests for evidence provenance, self-citation, and tool budgets."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from maintainer_lens.agents.investigation.evidence import to_history_evidence
from maintainer_lens.agents.investigation.tool_calling import run_search_tool_step
from maintainer_lens.agents.investigation_graph import build_investigation_graph

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def history_hit(**changes):
    values = dict(
        repository="org/repo",
        number=42,
        source_type="item",
        comment_id=None,
        title="An earlier issue",
        content="An observation about cleanup.",
        source_url="https://github.com/org/repo/issues/42",
        source_updated_at=datetime(2026, 10, 28, tzinfo=UTC),
        distance=0.1,
    )
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_url": "https://evil.example/org/repo/issues/42"},
        {"source_url": "https://github.com/another/repo/issues/42"},
        {"source_url": "https://github.com/org/repo/issues/43"},
        {"source_url": "https://github.com/org/repo/issues/42?untrusted=1"},
        {"source_type": "comment", "comment_id": 9},
        {"source_type": "comment", "comment_id": None},
        {"source_type": "item", "comment_id": 9},
        {"source_updated_at": datetime(2026, 10, 28)},
    ],
)
async def test_history_provenance_is_validated(changes):
    with pytest.raises(ValueError):
        to_history_evidence(history_hit(**changes), "org/repo")


async def test_valid_pr_comment_retains_its_original_anchor():
    hit = history_hit(
        source_type="comment",
        comment_id=9,
        source_url="https://github.com/org/repo/pull/42#issuecomment-9",
    )
    assert to_history_evidence(hit, "org/repo")["source_url"] == hit.source_url


async def test_current_issue_and_its_comments_are_not_independent_history():
    search = AsyncMock(
        return_value=(
            history_hit(),
            history_hit(
                source_type="comment",
                comment_id=9,
                source_url="https://github.com/org/repo/issues/42#issuecomment-9",
            ),
        )
    )
    analyzer = AsyncMock()
    graph = build_investigation_graph(search, analyzer=analyzer)
    result = await graph.ainvoke(
        {
            "repository": "org/repo",
            "issue_number": 42,
            "issue_title": "Cleanup fails",
            "issue_body": "Please investigate.",
        }
    )
    assert result["history_evidence"] == []
    assert result["preliminary_analysis"]["findings"] == []
    assert result["next_step"] == "needs_more_evidence"
    analyzer.assert_not_awaited()


@pytest.mark.parametrize("duplicate", [False, True])
async def test_tool_selection_has_output_budget_and_excludes_current_issue(duplicate):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            assert payload["max_tokens"] == 600
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_history",
                                            "arguments": json.dumps(
                                                {"query": "cleanup"}
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "No independent evidence."},
                    }
                ]
            },
        )

    search = AsyncMock(return_value=(history_hit(),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await run_search_tool_step(
            client,
            base_url="https://llm.example",
            api_key="fake-key",
            model_name="fake-model",
            repository="org/repo",
            question="How does cleanup work?",
            response_language="en",
            history_searcher=search,
            searched_queries=["cleanup"] if duplicate else [],
            excluded_issue_number=42,
        )
    assert result["history_evidence"] == []
    assert result["evidence"] == []
    assert result["skipped_duplicate"] is duplicate
    assert len(requests) == (1 if duplicate else 2)
    if duplicate:
        search.assert_not_awaited()
    else:
        search.assert_awaited_once()
