"""Tests for investigation retry deadline scheduling."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

import maintainer_lens.messaging.handler as handler_module
from maintainer_lens.messaging.schemas import (
    RETRY_ATTEMPTS_HEADER,
    RETRY_NOT_BEFORE_HEADER,
    InvestigationRequestedMessage,
)
from maintainer_lens.messaging.topology import RETRY_ROUTING_KEY
from maintainer_lens.services.issue_investigator import GitHubRateLimitError


@pytest.mark.anyio
async def test_retry_deadline_defers_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defer execution without consuming another retry attempt."""

    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()

    message = Mock()
    message.headers = {
        RETRY_ATTEMPTS_HEADER: 1,
        RETRY_NOT_BEFORE_HEADER: deadline,
    }

    exchange = Mock()
    processor = AsyncMock()
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    await handler_module.handle_investigation_message_with_retry(
        message=message,
        exchange=exchange,
        processor=processor,
    )

    processor.assert_not_awaited()

    republish.assert_awaited_once_with(
        exchange=exchange,
        incoming_message=message,
        routing_key=RETRY_ROUTING_KEY,
        retry_attempts=1,
        retry_not_before=deadline,
    )


@pytest.mark.anyio
async def test_expired_retry_deadline_allows_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute normally after the retry deadline has passed."""

    request = InvestigationRequestedMessage(
        task_id="task-001",
        repository="encode/httpx",
        issue_number=3712,
    )

    message = Mock()
    message.body = request.model_dump_json().encode()
    message.headers = {
        RETRY_ATTEMPTS_HEADER: 1,
        RETRY_NOT_BEFORE_HEADER: (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    }

    processor = AsyncMock()
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    await handler_module.handle_investigation_message_with_retry(
        message=message,
        exchange=Mock(),
        processor=processor,
    )

    processor.assert_awaited_once_with(request, False)
    republish.assert_not_awaited()


@pytest.mark.anyio
async def test_rate_limit_publishes_retry_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve the GitHub rate limit deadline in a retry message."""

    request = InvestigationRequestedMessage(
        task_id="task-001",
        repository="encode/httpx",
        issue_number=3712,
    )

    deadline = datetime.now(UTC) + timedelta(minutes=10)

    message = Mock()
    message.body = request.model_dump_json().encode()
    message.headers = {
        RETRY_ATTEMPTS_HEADER: 0,
    }

    processor = AsyncMock(side_effect=GitHubRateLimitError(deadline))

    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    await handler_module.handle_investigation_message_with_retry(
        message=message,
        exchange=Mock(),
        processor=processor,
    )

    processor.assert_awaited_once_with(request, False)

    republish.assert_awaited_once()

    kwargs = republish.await_args.kwargs

    assert kwargs["routing_key"] == RETRY_ROUTING_KEY
    assert kwargs["retry_attempts"] == 1
    assert kwargs["retry_not_before"] == deadline.isoformat()
