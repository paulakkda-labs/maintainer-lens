"""Opt-in live DeepSeek tool-calling integration test."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from maintainer_lens.agents.investigation.tool_calling import (
    run_search_tool_step,
)
from maintainer_lens.core.config import get_settings

pytestmark = pytest.mark.anyio

INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def test_live_deepseek_selects_and_uses_search_tool() -> None:
    if os.getenv("RUN_LIVE_LLM_TOOL_CALLING") != "1":
        pytest.skip("Live tool calling is disabled")

    settings = get_settings()

    configured_key = settings.llm_api_key
    api_key = (
        configured_key.get_secret_value()
        if isinstance(configured_key, SecretStr)
        else str(configured_key)
    )

    history_searcher = AsyncMock(
        return_value=(
            SimpleNamespace(
                repository="encode/httpx",
                number=3782,
                source_type="item",
                comment_id=None,
                title="Connection cancellation discussion",
                content=(
                    "Repeated cancellation may affect how "
                    "active connections are released."
                ),
                source_url=("https://github.com/encode/httpx/issues/3782"),
                source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
                distance=0.12,
            ),
        )
    )

    source_searcher = AsyncMock(
        return_value=[
            SimpleNamespace(
                index_id=INDEX_ID,
                chunk_key="chunk-001",
                repository="encode/httpx",
                commit_sha="a" * 40,
                blob_sha="b" * 40,
                file_path="httpx/_client.py",
                language="python",
                kind="code",
                symbol="send",
                start_line=100,
                end_line=120,
                content=(
                    "async def send(self, request):\n"
                    "    try:\n"
                    "        return await self._send(request)\n"
                    "    finally:\n"
                    "        await self._cleanup()\n"
                ),
                rrf_score=0.03,
                vector_rank=1,
                keyword_rank=1,
                cosine_distance=0.12,
                matched_in="content",
            )
        ]
    )

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(90.0, connect=10.0),
    ) as http_client:
        result = await run_search_tool_step(
            http_client,
            base_url=str(settings.llm_base_url),
            api_key=api_key,
            model_name=settings.llm_model_name,
            repository="encode/httpx",
            question=(
                "Investigate where AsyncClient handles "
                "connection cleanup during cancellation."
            ),
            response_language="zh",
            history_searcher=history_searcher,
            source_searcher=source_searcher,
            source_index_id=INDEX_ID,
        )

    assert result["tool_name"] in {
        "search_history",
        "search_source",
    }
    assert result["query"].strip()
    assert result["tool_call_id"]
    assert result["model_response"].strip()
    assert len(result["evidence"]) == 1

    if result["tool_name"] == "search_history":
        history_searcher.assert_awaited_once()
        source_searcher.assert_not_awaited()
    else:
        source_searcher.assert_awaited_once()
        history_searcher.assert_not_awaited()

    print("\n=== Live DeepSeek Tool Calling ===")
    print("Selected tool:", result["tool_name"])
    print("Search query:", result["query"])
    print("Tool call ID:", result["tool_call_id"])
    print("Evidence:", result["evidence"])
    print("Model response:", result["model_response"])
