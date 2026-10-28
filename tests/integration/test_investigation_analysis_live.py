"""Opt-in live DeepSeek analysis tests for LangGraph investigations."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from maintainer_lens.agents.investigation_analysis import (
    create_llm_analyzer,
)
from maintainer_lens.agents.investigation_graph import (
    build_investigation_graph,
)
from maintainer_lens.core.config import get_settings

pytestmark = pytest.mark.anyio

SOURCE_INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
COMMIT_SHA = "a" * 40


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _history_hit() -> SimpleNamespace:
    """Provide controlled historical evidence without GitHub access."""

    return SimpleNamespace(
        repository="encode/httpx",
        number=3782,
        source_type="item",
        comment_id=None,
        title="Connection cancellation",
        content=(
            "The historical discussion describes connections "
            "remaining active after repeated task cancellation."
        ),
        source_url="https://github.com/encode/httpx/issues/3782",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.1,
    )


def _source_hit() -> SimpleNamespace:
    """Provide a controlled source result without PostgreSQL access."""

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
        cosine_distance=0.1,
        matched_in="content",
    )


def _contains_chinese(value: str) -> bool:
    """Check for CJK unified ideographs in the generated summary."""

    return any("\u4e00" <= char <= "\u9fff" for char in value)


@pytest.mark.parametrize(
    "response_language",
    ["zh", "en"],
)
async def test_live_llm_investigation_analysis(
    response_language: str,
) -> None:
    if os.getenv("RUN_LIVE_LLM_ANALYSIS") != "1":
        pytest.skip("Live LLM integration is disabled")

    # Pydantic Settings reads the existing LLM_* values from .env.
    settings = get_settings()

    configured_key = settings.llm_api_key
    api_key = (
        configured_key.get_secret_value()
        if isinstance(configured_key, SecretStr)
        else str(configured_key)
    )

    history_searcher = AsyncMock(return_value=(_history_hit(),))
    source_searcher = AsyncMock(return_value=[_source_hit()])

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(90.0, connect=10.0),
    ) as http_client:
        analyzer = create_llm_analyzer(
            http_client,
            base_url=str(settings.llm_base_url),
            api_key=api_key,
            model_name=settings.llm_model_name,
            thinking_enabled=False,
        )

        graph = build_investigation_graph(
            history_searcher,
            source_searcher=source_searcher,
            analyzer=analyzer,
        )

        result = await graph.ainvoke(
            {
                "repository": "encode/httpx",
                "issue_number": 9000,
                "issue_title": "Connections remain active",
                "issue_body": (
                    "Repeated cancellation may leave connections "
                    "active in the connection pool."
                ),
                "source_index_id": str(SOURCE_INDEX_ID),
                "response_language": response_language,
            }
        )

    history_searcher.assert_awaited_once()
    source_searcher.assert_awaited_once()

    assert result["response_language"] == response_language
    assert len(result["history_evidence"]) == 1
    assert len(result["source_evidence"]) == 1

    report = result["preliminary_analysis"]

    assert isinstance(report["summary"], str)
    assert report["summary"].strip()
    assert isinstance(report["findings"], list)
    assert isinstance(report["hypotheses"], list)
    assert isinstance(report["unanswered_questions"], list)

    allowed_evidence_ids = {
        "history:item:3782",
        f"source:{SOURCE_INDEX_ID}:chunk-001",
    }

    for finding in report["findings"]:
        assert finding["statement"].strip()
        assert finding["evidence_ids"]
        assert set(finding["evidence_ids"]).issubset(allowed_evidence_ids)

    if response_language == "zh":
        assert _contains_chinese(report["summary"]), (
            "Expected a Simplified Chinese report summary"
        )
    else:
        assert not _contains_chinese(report["summary"]), (
            "Expected an English report summary"
        )

    print(f"\n=== Live LLM Analysis: {response_language} ===")
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )
    )
