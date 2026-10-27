"""Unit tests for investigation message handling."""

from unittest.mock import AsyncMock, Mock

import pytest

from maintainer_lens.messaging.handler import (
    InvalidInvestigationMessageError,
    handle_investigation_message,
)
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage


@pytest.mark.anyio
async def test_handler_dispatches_validated_message() -> None:
    """Pass a validated investigation request to the processor."""
    message = Mock(
        body=b'{"task_id":"task-001","repository":"fastapi/fastapi","issue_number":123}'
    )
    processor = AsyncMock()

    await handle_investigation_message(message, processor)

    processor.assert_awaited_once()

    request = processor.await_args.args[0]

    assert isinstance(request, InvestigationRequestedMessage)
    assert request.task_id == "task-001"
    assert request.repository == "fastapi/fastapi"
    assert request.issue_number == 123


@pytest.mark.anyio
async def test_handler_rejects_invalid_message() -> None:
    """Reject invalid messages without invoking the processor."""
    message = Mock(
        body=b'{"task_id":"task-001","repository":"fastapi/fastapi","issue_number":-1}'
    )
    processor = AsyncMock()

    with pytest.raises(InvalidInvestigationMessageError):
        await handle_investigation_message(message, processor)

    processor.assert_not_awaited()


@pytest.mark.anyio
async def test_handler_propagates_processor_failure() -> None:
    """Propagate processor failures to the consumer."""
    message = Mock(
        body=b'{"task_id":"task-001","repository":"fastapi/fastapi","issue_number":123}'
    )
    processor = AsyncMock(side_effect=RuntimeError("Investigation processing failed"))

    with pytest.raises(RuntimeError, match="Investigation processing failed"):
        await handle_investigation_message(message, processor)

    processor.assert_awaited_once()
