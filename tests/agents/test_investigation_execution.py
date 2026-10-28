"""Tests for bounded and observable investigation tool execution."""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from maintainer_lens.agents.investigation.execution import (
    with_tool_step_controls,
)
from maintainer_lens.agents.investigation.tool_calling import (
    SearchToolStepResult,
)
from maintainer_lens.agents.investigation_analysis import (
    AnalysisFinding,
    InvestigationAnalysis,
)
from maintainer_lens.agents.investigation_graph import (
    build_investigation_graph,
)

pytestmark = pytest.mark.anyio

LOG_NAME = "maintainer_lens.agents.investigation.execution"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _state() -> dict:
    return {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "Connection cancellation",
        "issue_body": "PRIVATE_ISSUE_CONTENT_DO_NOT_LOG",
        "response_language": "en",
        "search_round": 2,
        "searched_queries": ["connection cancellation"],
    }


def _tool_result(
    *,
    skipped: bool = False,
) -> SearchToolStepResult:
    return {
        "tool_name": "search_history",
        "tool_call_id": "call_test_123",
        "query": "CancelledError cleanup",
        "evidence": [],
        "history_evidence": [],
        "source_evidence": [],
        "skipped_duplicate": skipped,
        "model_response": "PRIVATE_MODEL_CONTENT_DO_NOT_LOG",
    }


def _execution_records(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "investigation_tool_step"
    ]


async def test_swallowed_tool_deadline_cancellation_cannot_return_success(caplog):
    swallowed = asyncio.Event()

    async def runner(state):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            return _tool_result()

    controlled = with_tool_step_controls(runner, timeout_seconds=0.02)
    with caplog.at_level(logging.WARNING, logger=LOG_NAME):
        with pytest.raises(TimeoutError, match="deadline exceeded"):
            await controlled(_state())
    assert swallowed.is_set()
    assert _execution_records(caplog)[0].outcome == "timed_out"


async def test_success_returns_original_result_and_logs_metadata(
    caplog,
) -> None:
    expected = _tool_result()
    runner = AsyncMock(return_value=expected)
    controlled = with_tool_step_controls(
        runner,
        timeout_seconds=10.0,
    )
    state = _state()

    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        actual = await controlled(state)

    assert actual is expected
    runner.assert_awaited_once_with(state)

    records = _execution_records(caplog)
    assert len(records) == 1

    record = records[0]
    assert record.outcome == "succeeded"
    assert record.search_round == 2
    assert record.tool_name == "search_history"
    assert record.duration_ms >= 0
    assert record.error_type is None
    assert record.levelno == logging.INFO

    assert "PRIVATE_ISSUE_CONTENT_DO_NOT_LOG" not in caplog.text
    assert "PRIVATE_MODEL_CONTENT_DO_NOT_LOG" not in caplog.text


async def test_skipped_query_is_not_logged_as_failure(caplog) -> None:
    runner = AsyncMock(return_value=_tool_result(skipped=True))
    controlled = with_tool_step_controls(runner)

    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        result = await controlled(_state())

    assert result["skipped_duplicate"] is True

    records = _execution_records(caplog)
    assert len(records) == 1
    assert records[0].outcome == "skipped"
    assert records[0].levelno == logging.INFO


async def test_runner_failure_preserves_exception_without_leaking_message(
    caplog,
) -> None:
    runner = AsyncMock(side_effect=RuntimeError("PRIVATE_ERROR_MESSAGE_DO_NOT_LOG"))
    controlled = with_tool_step_controls(runner)

    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        with pytest.raises(
            RuntimeError,
            match="PRIVATE_ERROR_MESSAGE_DO_NOT_LOG",
        ):
            await controlled(_state())

    records = _execution_records(caplog)
    assert len(records) == 1

    assert records[0].outcome == "failed"
    assert records[0].error_type == "RuntimeError"
    assert records[0].tool_name is None
    assert records[0].levelno == logging.WARNING

    assert "PRIVATE_ERROR_MESSAGE_DO_NOT_LOG" not in caplog.text


async def test_own_deadline_reports_timeout(caplog) -> None:
    async def blocked_runner(state):
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")

    controlled = with_tool_step_controls(
        blocked_runner,
        timeout_seconds=0.02,
    )

    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        with pytest.raises(TimeoutError):
            await controlled(_state())

    records = _execution_records(caplog)
    assert len(records) == 1

    assert records[0].outcome == "timed_out"
    assert records[0].error_type == "TimeoutError"


async def test_upstream_timeout_is_classified_as_failure(caplog) -> None:
    runner = AsyncMock(side_effect=TimeoutError("Upstream timeout"))
    controlled = with_tool_step_controls(
        runner,
        timeout_seconds=10.0,
    )

    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        with pytest.raises(
            TimeoutError,
            match="Upstream timeout",
        ):
            await controlled(_state())

    records = _execution_records(caplog)
    assert len(records) == 1

    assert records[0].outcome == "failed"
    assert records[0].error_type == "TimeoutError"


async def test_external_cancellation_is_propagated(caplog) -> None:
    started = asyncio.Event()
    never_finished = asyncio.Event()

    async def blocked_runner(state):
        started.set()
        await never_finished.wait()

    controlled = with_tool_step_controls(
        blocked_runner,
        timeout_seconds=10.0,
    )

    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        task = asyncio.create_task(controlled(_state()))

        await asyncio.wait_for(started.wait(), timeout=1.0)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    records = _execution_records(caplog)
    assert len(records) == 1

    assert records[0].outcome == "cancelled"
    assert records[0].error_type == "CancelledError"


@pytest.mark.parametrize(
    "timeout_seconds",
    [
        0,
        -1,
        True,
        float("nan"),
        float("inf"),
        "120",
    ],
)
def test_invalid_execution_budget_is_rejected(
    timeout_seconds,
) -> None:
    with pytest.raises(
        ValueError,
        match="timeout_seconds must be finite and positive",
    ):
        with_tool_step_controls(
            AsyncMock(),
            timeout_seconds=timeout_seconds,
        )


async def test_wrapped_runner_works_inside_real_langgraph(
    caplog,
) -> None:
    history_hit = SimpleNamespace(
        repository="encode/httpx",
        number=3782,
        source_type="item",
        comment_id=None,
        title="Cancellation discussion",
        content="Cancellation may affect connection cleanup.",
        source_url="https://github.com/encode/httpx/issues/3782",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.12,
    )

    history_searcher = AsyncMock(return_value=(history_hit,))

    analyzer = AsyncMock(
        return_value=InvestigationAnalysis(
            summary="Further investigation is required.",
            findings=[
                AnalysisFinding(
                    statement="Historical evidence mentions cancellation.",
                    evidence_ids=["history:item:3782"],
                )
            ],
            hypotheses=[],
            unanswered_questions=["How is the connection released?"],
        )
    )

    tool_runner = AsyncMock(return_value=_tool_result(skipped=True))

    controlled_runner = with_tool_step_controls(
        tool_runner,
        timeout_seconds=10.0,
    )

    graph = build_investigation_graph(
        history_searcher,
        analyzer=analyzer,
        tool_search_runner=controlled_runner,
        max_search_rounds=2,
    )

    initial_state = {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "Connection cancellation",
        "issue_body": "Connections remain active.",
        "response_language": "en",
    }

    with caplog.at_level(logging.INFO, logger=LOG_NAME):
        result = await graph.ainvoke(initial_state)

    history_searcher.assert_awaited_once()
    analyzer.assert_awaited_once()
    tool_runner.assert_awaited_once()

    assert result["search_round"] == 1
    assert result["tool_search_skipped"] is True
    assert result["next_step"] == "needs_more_evidence"

    records = _execution_records(caplog)
    assert len(records) == 1

    assert records[0].outcome == "skipped"
    assert records[0].search_round == 1
    assert records[0].tool_name == "search_history"
