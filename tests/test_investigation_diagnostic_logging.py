"""Offline task correlation, safe diagnostics and original lifecycle semantics."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

import maintainer_lens.core.investigation_logging as diagnostic_logging
import maintainer_lens.investigation_runner as runner
import maintainer_lens.services.investigation_progress as progress_module
import maintainer_lens.worker as worker
from maintainer_lens.agents.investigation_analysis import InvestigationAnalysis
from maintainer_lens.core.investigation_logging import (
    investigation_log_context,
    log_investigation_event,
)
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage

pytestmark = pytest.mark.anyio
LOG_NAME = "maintainer_lens.core.investigation_logging"
SECRET = "fake-secret-never-log"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _messages(caplog):
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == LOG_NAME
    ]


@pytest.fixture
def lifecycle(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    scope = SimpleNamespace(active=0, fail_commit=False)

    class Session:
        @asynccontextmanager
        async def begin(self):
            scope.active += 1
            try:
                yield
                if scope.fail_commit:
                    raise RuntimeError(SECRET)
            finally:
                scope.active -= 1

    @asynccontextmanager
    async def factory():
        yield Session()

    scope.factory = factory
    for name in (
        "claim_investigation_task",
        "mark_investigation_succeeded",
        "mark_investigation_failed",
        "release_investigation_task_for_retry",
    ):
        mock = AsyncMock(return_value=SECRET if name.startswith("claim") else True)
        monkeypatch.setattr(runner, name, mock)
        setattr(scope, name, mock)
    monkeypatch.setattr(runner, "maintain_investigation_lease", AsyncMock())
    scope.append = AsyncMock()
    monkeypatch.setattr(progress_module, "append_task_event", scope.append)
    return scope


def _binding(task_id="task", number=200):
    return investigation_log_context(
        task_id=task_id, repository="owner/repo", issue_number=number, generation=1
    )


async def test_original_validation_path_is_logged_without_input_or_exception_text(
    lifecycle, caplog
):
    reporter = progress_module.InvestigationProgressReporter(
        lifecycle.factory, "task", SECRET
    )

    async def processor(claim_token):
        assert claim_token == SECRET
        await reporter.phase("analyzing", 1)
        InvestigationAnalysis.model_validate(
            {
                "summary": SECRET,
                "findings": [{"statement": SECRET, "evidence_ids": []}],
                "hypotheses": [],
                "unanswered_questions": [],
            }
        )

    with _binding(), pytest.raises(runner.PermanentInvestigationError):
        await runner.execute_investigation_task(
            lifecycle.factory, "task", processor, expected_generation=1
        )
    messages = _messages(caplog)
    assert [item["event"] for item in messages] == [
        "claim_committed",
        "phase_committed",
        "processor_failed",
        "failure_committed",
    ]
    failure = messages[2]
    assert failure["repository"] == "owner/repo" and failure["issue_number"] == 200
    assert failure["generation"] == failure["search_round"] == 1
    assert failure["phase"] == "analyzing" and failure["retryable"] is False
    assert failure["error"]["validation"] == [
        {"location": ["findings", 0, "evidence_ids"], "type": "too_short"}
    ]
    assert any(frame["function"] == "processor" for frame in failure["error"]["frames"])
    assert SECRET not in caplog.text
    lifecycle.mark_investigation_failed.assert_awaited_once()
    lifecycle.mark_investigation_succeeded.assert_not_awaited()


@pytest.mark.parametrize("permanent", [False, True])
async def test_business_exception_identity_and_retry_policy_survive_logging(
    lifecycle, caplog, permanent
):
    error = (
        runner.PermanentInvestigationError(SECRET)
        if permanent
        else runner.RetryableInvestigationError(SECRET)
    )
    with _binding(), pytest.raises(type(error)) as caught:
        await runner.execute_investigation_task(
            lifecycle.factory, "task", AsyncMock(side_effect=error)
        )
    assert caught.value is error
    messages = _messages(caplog)
    assert messages[-1]["event"] == (
        "failure_committed" if permanent else "retry_committed"
    )
    assert messages[-2]["retryable"] is (not permanent)
    assert SECRET not in caplog.text


async def test_failure_diagnostic_exists_even_if_status_commit_fails(lifecycle, caplog):
    async def processor(claim_token):
        lifecycle.fail_commit = True
        raise ValueError(SECRET)

    with _binding(), pytest.raises(RuntimeError):
        await runner.execute_investigation_task(lifecycle.factory, "task", processor)
    assert any(item["event"] == "processor_failed" for item in _messages(caplog))
    assert not any(item["event"] == "failure_committed" for item in _messages(caplog))
    assert SECRET not in caplog.text


async def test_diagnostic_sink_failure_does_not_replace_business_error(
    lifecycle, monkeypatch
):
    error = runner.RetryableInvestigationError(SECRET)
    monkeypatch.setattr(
        diagnostic_logging.logger,
        "log",
        Mock(side_effect=RuntimeError("sink unavailable")),
    )
    with _binding(), pytest.raises(runner.RetryableInvestigationError) as caught:
        await runner.execute_investigation_task(
            lifecycle.factory, "task", AsyncMock(side_effect=error)
        )
    assert caught.value is error
    lifecycle.release_investigation_task_for_retry.assert_awaited_once()


async def test_failed_phase_commit_is_not_logged_as_committed(lifecycle, caplog):
    lifecycle.fail_commit = True
    reporter = progress_module.InvestigationProgressReporter(
        lifecycle.factory, "task", SECRET
    )
    with _binding(), pytest.raises(RuntimeError):
        await reporter.phase("analyzing", 1)
    assert _messages(caplog) == []


@pytest.mark.parametrize("fail_commit", [False, True])
async def test_completion_log_requires_successful_status_commit(
    lifecycle, caplog, fail_commit
):
    async def processor(claim_token):
        lifecycle.fail_commit = fail_commit

    with _binding():
        if fail_commit:
            with pytest.raises(RuntimeError):
                await runner.execute_investigation_task(
                    lifecycle.factory, "task", processor
                )
        else:
            await runner.execute_investigation_task(
                lifecycle.factory, "task", processor
            )
    assert any(
        item["event"] == "completion_committed" for item in _messages(caplog)
    ) is (not fail_commit)


async def test_external_cancellation_propagates_and_does_not_mark_task_terminal(
    lifecycle, caplog
):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def processor(claim_token):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def execute():
        with _binding():
            await runner.execute_investigation_task(
                lifecycle.factory, "task", processor
            )

    operation = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
    assert cleaned.is_set()
    assert [item["event"] for item in _messages(caplog)] == [
        "claim_committed",
        "execution_interrupted",
    ]
    lifecycle.mark_investigation_failed.assert_not_awaited()
    lifecycle.mark_investigation_succeeded.assert_not_awaited()
    lifecycle.release_investigation_task_for_retry.assert_not_awaited()


async def test_lease_lost_group_is_not_reclassified_by_diagnostics(lifecycle, caplog):
    original = runner.LeaseLostError(SECRET)
    with _binding(), pytest.raises(ExceptionGroup) as caught:
        await runner.execute_investigation_task(
            lifecycle.factory, "task", AsyncMock(side_effect=original)
        )
    assert caught.value.exceptions == (original,)
    assert _messages(caplog)[-1]["error"]["children"][0]["type"] == "LeaseLostError"
    lifecycle.mark_investigation_failed.assert_not_awaited()
    lifecycle.release_investigation_task_for_retry.assert_not_awaited()
    assert SECRET not in caplog.text


async def test_concurrent_delivery_scopes_and_descendants_do_not_mix(caplog):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    first, second = asyncio.Event(), asyncio.Event()

    async def execute(task_id, number, own, other):
        with _binding(task_id, number):
            own.set()
            await other.wait()

            async def graph_child():
                log_investigation_event(
                    "phase_committed",
                    task_id=task_id,
                    phase="analyzing",
                    search_round=2,
                )

            await asyncio.create_task(graph_child())
            log_investigation_event(
                "processor_failed", task_id=task_id, error=ValueError()
            )

    await asyncio.wait_for(
        asyncio.gather(execute("a", 1, first, second), execute("b", 2, second, first)),
        2,
    )
    log_investigation_event("outside", task_id="a")
    for item in _messages(caplog)[:-1]:
        assert item["issue_number"] == {"a": 1, "b": 2}[item["task_id"]]
        assert item["phase"] == "analyzing" and item["search_round"] == 2
    assert _messages(caplog)[-1]["repository"] is None


@pytest.mark.parametrize(
    "failure", [None, RuntimeError(SECRET), asyncio.CancelledError()]
)
async def test_worker_handler_binds_and_restores_context(monkeypatch, caplog, failure):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    request = InvestigationRequestedMessage(
        task_id="inner", repository="other/repo", issue_number=7, generation=2
    )

    async def process(**kwargs):
        assert kwargs["request"] == request
        log_investigation_event("bound", task_id="inner")
        if failure is not None:
            raise failure

    monkeypatch.setattr(worker, "process_investigation_request", process)
    handler = worker.build_investigation_message_handler(
        object(), object(), AsyncMock()
    )
    message = SimpleNamespace(body=request.model_dump_json().encode(), headers={})
    with _binding("outer"):
        if failure is None:
            await handler(message)
        else:
            with pytest.raises(type(failure)) as caught:
                await handler(message)
            assert caught.value is failure
        log_investigation_event("restored", task_id="outer")
    bound, restored = _messages(caplog)
    assert (bound["repository"], bound["issue_number"], bound["generation"]) == (
        "other/repo",
        7,
        2,
    )
    assert restored["repository"] == "owner/repo" and restored["generation"] == 1


def test_diagnostics_redact_unknown_validation_keys_and_never_format_exceptions(caplog):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    try:
        InvestigationAnalysis.model_validate(
            {
                "summary": "Uncertain",
                "findings": [],
                "hypotheses": [],
                "unanswered_questions": [],
                SECRET: SECRET,
            }
        )
    except ValidationError as exc:
        validation = exc

    class UnprintableError(Exception):
        def __str__(self):
            raise AssertionError("Exception text must not be formatted")

        def __repr__(self):
            raise AssertionError("Exception values must not be captured")

    error = UnprintableError(SECRET)
    error.__cause__ = validation
    log_investigation_event("processor_failed", task_id="task", error=error)
    diagnostic = _messages(caplog)[0]["error"]
    assert diagnostic["cause"]["validation"] == [
        {"location": ["<field>"], "type": "extra_forbidden"}
    ]
    assert SECRET not in caplog.text


def test_group_diagnostics_are_bounded_and_do_not_mutate_the_group(caplog):
    caplog.set_level(logging.INFO, logger=LOG_NAME)
    children = [ValueError(SECRET) for _ in range(30)]
    group = ExceptionGroup(SECRET, children)
    group.__cause__ = group
    log_investigation_event("processor_failed", task_id="task", error=group)
    diagnostic = _messages(caplog)[0]["error"]
    assert len(diagnostic["children"]) == 5 and diagnostic["omitted_children"] == 25
    assert diagnostic["cause"] == {"truncated": True}
    assert group.exceptions == tuple(children) and group.__cause__ is group
    assert SECRET not in caplog.text
