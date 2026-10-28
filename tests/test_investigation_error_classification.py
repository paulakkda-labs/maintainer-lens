"""Exercise error classification through the existing task runner."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import maintainer_lens.investigation_runner as runner

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def operations(monkeypatch):
    session = MagicMock()
    session.__aenter__.return_value = session
    factory = MagicMock(return_value=session)
    claim = AsyncMock(return_value="claim-token")
    release = AsyncMock(return_value=True)
    fail = AsyncMock(return_value=True)
    succeed = AsyncMock(return_value=True)
    monkeypatch.setattr(runner, "claim_investigation_task", claim)
    monkeypatch.setattr(runner, "release_investigation_task_for_retry", release)
    monkeypatch.setattr(runner, "mark_investigation_failed", fail)
    monkeypatch.setattr(runner, "mark_investigation_succeeded", succeed)
    return factory, release, fail, succeed


@pytest.mark.parametrize("final_attempt", [False, True])
async def test_nested_temporary_errors_use_existing_retry_budget(
    operations, final_attempt
):
    factory, release, fail, succeed = operations
    error = ExceptionGroup(
        "parallel", [ExceptionGroup("inner", [TimeoutError("timeout")])]
    )
    with pytest.raises(runner.RetryableInvestigationError):
        await runner.execute_investigation_task(
            factory,
            "task",
            AsyncMock(side_effect=error),
            final_attempt=final_attempt,
        )
    assert fail.await_count == int(final_attempt)
    assert release.await_count == int(not final_attempt)
    succeed.assert_not_awaited()


async def test_permanent_error_in_nested_group_prevents_retry(operations):
    factory, release, fail, succeed = operations
    error = ExceptionGroup(
        "parallel", [TimeoutError(), ExceptionGroup("inner", [ValueError()])]
    )
    with pytest.raises(runner.PermanentInvestigationError):
        await runner.execute_investigation_task(
            factory, "task", AsyncMock(side_effect=error)
        )
    fail.assert_awaited_once()
    release.assert_not_awaited()
    succeed.assert_not_awaited()


async def test_group_retains_latest_rate_limit_deadline(operations):
    factory, release, _, _ = operations
    early = runner.RetryableInvestigationError("first limit")
    early.not_before = datetime.now(UTC) + timedelta(minutes=1)
    late = runner.RetryableInvestigationError("second limit")
    late.not_before = early.not_before + timedelta(minutes=1)
    with pytest.raises(runner.RetryableInvestigationError) as captured:
        await runner.execute_investigation_task(
            factory,
            "task",
            AsyncMock(
                side_effect=ExceptionGroup("limits", [early, late]),
            ),
        )
    assert captured.value.not_before == late.not_before
    release.assert_awaited_once()


async def test_nested_lease_loss_never_updates_task_state(operations):
    factory, release, fail, succeed = operations
    error = ExceptionGroup(
        "outer", [runner.LeaseLostError("lost"), ValueError("bad data")]
    )
    with pytest.raises(ExceptionGroup):
        await runner.execute_investigation_task(
            factory, "task", AsyncMock(side_effect=error)
        )
    release.assert_not_awaited()
    fail.assert_not_awaited()
    succeed.assert_not_awaited()


async def test_processor_cancellation_is_not_a_retryable_business_error(operations):
    factory, release, fail, succeed = operations
    with pytest.raises(asyncio.CancelledError):
        await runner.execute_investigation_task(
            factory,
            "task",
            AsyncMock(
                side_effect=asyncio.CancelledError(),
            ),
        )
    release.assert_not_awaited()
    fail.assert_not_awaited()
    succeed.assert_not_awaited()
