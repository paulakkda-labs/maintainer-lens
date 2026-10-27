"""Opt-in live DeepSeek query planning integration test."""

import os

import httpx
import pytest
from pydantic import SecretStr

from maintainer_lens.agents.investigation.query_planning import (
    create_llm_query_planner,
    normalize_query,
)
from maintainer_lens.core.config import get_settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_live_llm_follow_up_query_planning() -> None:
    if os.getenv("RUN_LIVE_LLM_QUERY_PLANNING") != "1":
        pytest.skip("Live query planning is disabled")

    settings = get_settings()

    configured_key = settings.llm_api_key
    api_key = (
        configured_key.get_secret_value()
        if isinstance(configured_key, SecretStr)
        else str(configured_key)
    )

    previous_query = "Connections remain active after repeated cancellation"

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(90.0, connect=10.0),
    ) as client:
        planner = create_llm_query_planner(
            client,
            base_url=str(settings.llm_base_url),
            api_key=api_key,
            model_name=settings.llm_model_name,
            thinking_enabled=False,
        )

        plan = await planner(
            {
                "repository": "encode/httpx",
                "issue_title": "Connections remain active",
                "issue_body": (
                    "Connections may remain active after repeated "
                    "async task cancellation."
                ),
                "current_query": previous_query,
                "searched_queries": [previous_query],
                "summary": (
                    "Historical evidence suggests that cancellation "
                    "may affect connection cleanup, but the root cause "
                    "has not been established."
                ),
                "unanswered_questions": [
                    "How does cancellation interact with connection pool cleanup?"
                ],
                "hypotheses": ["Cleanup may be interrupted during cancellation."],
                "response_language": "zh",
            }
        )

    assert plan.reason.strip()

    if plan.query is not None:
        assert plan.query.strip()
        assert len(plan.query) <= 256
        assert normalize_query(plan.query) != normalize_query(previous_query)

    print("\n=== Live Query Planning ===")
    print("Query:", plan.query)
    print("Reason:", plan.reason)
