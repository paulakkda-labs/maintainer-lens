"""Tests for investigation request processing and retry decisions."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import maintainer_lens.messaging.handler as handler_module
from maintainer_lens.investigation_runner import (
    InvestigationClaimUnavailableError,
)
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage


@pytest.fixture
def investigation_request() -> InvestigationRequestedMessage:
    """Create a valid investigation request."""
    return InvestigationRequestedMessage(
        task_id="task-001",
        repository="fastapi/fastapi",
        issue_number=123,
    )


@pytest.mark.anyio
async def test_successful_execution_returns_normally(
    investigation_request: InvestigationRequestedMessage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return normally without querying status when execution succeeds."""
    execute = AsyncMock()
    get_status = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "execute_investigation_task",
        execute,
    )
    monkeypatch.setattr(
        handler_module,
        "get_investigation_task_status",
        get_status,
    )

    session_factory = MagicMock()
    processor = AsyncMock()

    await handler_module.process_investigation_request(
        request=investigation_request,
        session_factory=session_factory,
        processor=processor,
    )

    execute.assert_awaited_once_with(
        session_factory=session_factory,
        task_id="task-001",
        processor=processor,
        final_attempt=False,
    )

    get_status.assert_not_awaited()
    session_factory.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    ["succeeded"],
)
async def test_terminal_task_status_returns_normally(
    investigation_request: InvestigationRequestedMessage,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """Treat duplicate messages for terminal tasks as completed."""
    execute = AsyncMock(
        side_effect=InvestigationClaimUnavailableError("Task cannot be claimed")
    )
    get_status = AsyncMock(return_value=status)

    monkeypatch.setattr(
        handler_module,
        "execute_investigation_task",
        execute,
    )
    monkeypatch.setattr(
        handler_module,
        "get_investigation_task_status",
        get_status,
    )

    session_factory = MagicMock()
    session = session_factory.return_value.__aenter__.return_value

    await handler_module.process_investigation_request(
        request=investigation_request,
        session_factory=session_factory,
        processor=AsyncMock(),
    )

    execute.assert_awaited_once()

    get_status.assert_awaited_once_with(
        session=session,
        task_id="task-001",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "running",
        None,
        "unexpected-status",
    ],
)
async def test_nonterminal_task_requires_retry(
    investigation_request: InvestigationRequestedMessage,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
) -> None:
    """Request retry with structured metadata for nonterminal tasks."""
    claim_error = InvestigationClaimUnavailableError("Task cannot be claimed")

    execute = AsyncMock(side_effect=claim_error)
    get_status = AsyncMock(return_value=status)

    monkeypatch.setattr(
        handler_module,
        "execute_investigation_task",
        execute,
    )
    monkeypatch.setattr(
        handler_module,
        "get_investigation_task_status",
        get_status,
    )

    session_factory = MagicMock()
    session = session_factory.return_value.__aenter__.return_value

    with pytest.raises(handler_module.RetryableMessageError) as exc_info:
        await handler_module.process_investigation_request(
            request=investigation_request,
            session_factory=session_factory,
            processor=AsyncMock(),
        )

    error = exc_info.value

    # Structured metadata for consumer retry decisions.
    assert error.task_id == "task-001"
    assert error.status == status

    # Preserve the original claim failure.
    assert error.__cause__ is claim_error

    # Keep a readable error message for diagnostics.
    assert "task-001" in str(error)
    assert str(status) in str(error)

    get_status.assert_awaited_once_with(
        session=session,
        task_id="task-001",
    )


@pytest.mark.anyio
async def test_execution_failure_propagates(
    investigation_request: InvestigationRequestedMessage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagate execution failures without converting them to retry errors."""
    execution_error = RuntimeError("Database unavailable")

    execute = AsyncMock(side_effect=execution_error)
    get_status = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "execute_investigation_task",
        execute,
    )
    monkeypatch.setattr(
        handler_module,
        "get_investigation_task_status",
        get_status,
    )

    session_factory = MagicMock()

    with pytest.raises(RuntimeError) as exc_info:
        await handler_module.process_investigation_request(
            request=investigation_request,
            session_factory=session_factory,
            processor=AsyncMock(),
        )

    assert exc_info.value is execution_error
    get_status.assert_not_awaited()
    session_factory.assert_not_called()


@pytest.mark.anyio
async def test_status_lookup_failure_propagates(
    investigation_request: InvestigationRequestedMessage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagate database lookup errors instead of treating them as success."""
    execute = AsyncMock(
        side_effect=InvestigationClaimUnavailableError("Task cannot be claimed")
    )
    get_status = AsyncMock(side_effect=RuntimeError("Simulated database failure"))

    monkeypatch.setattr(
        handler_module,
        "execute_investigation_task",
        execute,
    )
    monkeypatch.setattr(
        handler_module,
        "get_investigation_task_status",
        get_status,
    )

    session_factory = MagicMock()

    with pytest.raises(
        RuntimeError,
        match="Simulated database failure",
    ):
        await handler_module.process_investigation_request(
            request=investigation_request,
            session_factory=session_factory,
            processor=AsyncMock(),
        )

    get_status.assert_awaited_once()
