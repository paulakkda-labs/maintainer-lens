"""Tests for investigation task and lease coordination."""

import asyncio
from unittest.mock import Mock

import pytest

import maintainer_lens.investigation_runner as runner_module


@pytest.mark.anyio
async def test_investigation_completion_stops_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop lease maintenance when investigation completes."""
    lease_started = asyncio.Event()
    lease_stopped = asyncio.Event()

    async def fake_lease(
        *,
        stop_event: asyncio.Event,
        **_: object,
    ) -> None:
        lease_started.set()
        await stop_event.wait()
        lease_stopped.set()

    async def processor() -> None:
        await lease_started.wait()

    monkeypatch.setattr(
        runner_module,
        "maintain_investigation_lease",
        fake_lease,
    )

    await asyncio.wait_for(
        runner_module.run_investigation_with_lease(
            session_factory=Mock(),
            task_id="task-001",
            claim_token="token-001",
            processor=processor,
            renewal_interval_seconds=0.01,
        ),
        timeout=2,
    )

    assert lease_started.is_set()
    assert lease_stopped.is_set()


@pytest.mark.anyio
async def test_investigation_failure_stops_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop lease maintenance when investigation fails."""
    lease_started = asyncio.Event()
    lease_stopped = asyncio.Event()

    async def fake_lease(
        *,
        stop_event: asyncio.Event,
        **_: object,
    ) -> None:
        lease_started.set()

        try:
            await stop_event.wait()
        finally:
            lease_stopped.set()

    async def processor() -> None:
        await lease_started.wait()
        raise ValueError("Investigation failed")

    monkeypatch.setattr(
        runner_module,
        "maintain_investigation_lease",
        fake_lease,
    )

    with pytest.raises(ExceptionGroup) as exc_info:
        await asyncio.wait_for(
            runner_module.run_investigation_with_lease(
                session_factory=Mock(),
                task_id="task-002",
                claim_token="token-002",
                processor=processor,
                renewal_interval_seconds=0.01,
            ),
            timeout=2,
        )

    assert any(isinstance(exc, ValueError) for exc in exc_info.value.exceptions)
    assert lease_stopped.is_set()


@pytest.mark.anyio
async def test_lease_loss_cancels_investigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel the investigation when lease ownership is lost."""
    processor_started = asyncio.Event()
    processor_cancelled = asyncio.Event()

    async def processor() -> None:
        processor_started.set()

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            processor_cancelled.set()
            raise

    async def fake_lease(
        *,
        stop_event: asyncio.Event,
        **_: object,
    ) -> None:
        await processor_started.wait()

        assert not stop_event.is_set()

        raise runner_module.LeaseLostError("Simulated lease ownership loss")

    monkeypatch.setattr(
        runner_module,
        "maintain_investigation_lease",
        fake_lease,
    )

    with pytest.raises(ExceptionGroup) as exc_info:
        await asyncio.wait_for(
            runner_module.run_investigation_with_lease(
                session_factory=Mock(),
                task_id="task-003",
                claim_token="token-003",
                processor=processor,
                renewal_interval_seconds=0.01,
            ),
            timeout=2,
        )

    assert any(
        isinstance(exc, runner_module.LeaseLostError)
        for exc in exc_info.value.exceptions
    )

    assert processor_cancelled.is_set()


@pytest.mark.anyio
async def test_external_cancellation_stops_both_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean up investigation and lease tasks on external cancellation."""
    processor_started = asyncio.Event()
    processor_stopped = asyncio.Event()
    lease_started = asyncio.Event()
    lease_stopped = asyncio.Event()

    async def processor() -> None:
        processor_started.set()

        try:
            await asyncio.Event().wait()
        finally:
            processor_stopped.set()

    async def fake_lease(
        *,
        stop_event: asyncio.Event,
        **_: object,
    ) -> None:
        lease_started.set()

        try:
            await stop_event.wait()
        finally:
            lease_stopped.set()

    monkeypatch.setattr(
        runner_module,
        "maintain_investigation_lease",
        fake_lease,
    )

    runner = asyncio.create_task(
        runner_module.run_investigation_with_lease(
            session_factory=Mock(),
            task_id="task-004",
            claim_token="token-004",
            processor=processor,
            renewal_interval_seconds=0.01,
        )
    )

    try:
        await asyncio.wait_for(
            asyncio.gather(
                processor_started.wait(),
                lease_started.wait(),
            ),
            timeout=2,
        )
    finally:
        runner.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner, timeout=2)

    assert processor_stopped.is_set()
    assert lease_stopped.is_set()
