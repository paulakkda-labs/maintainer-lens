"""Tests for investigation message retry handling."""

from unittest.mock import AsyncMock, Mock

import pytest

import maintainer_lens.messaging.handler as handler_module
from maintainer_lens.messaging.schemas import (
    MISSING_TASK_CHECKS_HEADER,
    RETRY_ATTEMPTS_HEADER,
)
from maintainer_lens.messaging.topology import (
    DEAD_ROUTING_KEY,
    RETRY_ROUTING_KEY,
)


@pytest.fixture
def incoming_message() -> Mock:
    """Create a mock incoming RabbitMQ message."""
    message = Mock()
    message.headers = {}
    message.ack = AsyncMock()
    return message


@pytest.mark.anyio
async def test_successful_handling_does_not_republish(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not republish an investigation handled successfully."""
    handle = AsyncMock()
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    exchange = Mock()
    processor = AsyncMock()

    await handler_module.handle_investigation_message_with_retry(
        message=incoming_message,
        exchange=exchange,
        processor=processor,
    )

    handle.assert_awaited_once()

    assert handle.await_args.kwargs["message"] is incoming_message
    assert callable(handle.await_args.kwargs["processor"])

    republish.assert_not_awaited()
    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "headers", "expected_attempts"),
    [
        pytest.param("pending", {}, 0, id="pending-first-attempt"),
        pytest.param(
            "running",
            {RETRY_ATTEMPTS_HEADER: 2},
            2,
            id="running-preserves-attempts",
        ),
        pytest.param("running", None, 0, id="missing-headers"),
    ],
)
async def test_unavailable_task_is_republished(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    headers: dict[str, int] | None,
    expected_attempts: int,
) -> None:
    """Reschedule pending or running tasks without increasing retries."""
    incoming_message.headers = headers

    error = handler_module.RetryableMessageError(
        task_id="task-001",
        status=status,
    )

    handle = AsyncMock(side_effect=error)
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    exchange = Mock()

    await handler_module.handle_investigation_message_with_retry(
        message=incoming_message,
        exchange=exchange,
        processor=AsyncMock(),
    )

    republish.assert_awaited_once_with(
        exchange=exchange,
        incoming_message=incoming_message,
        routing_key=RETRY_ROUTING_KEY,
        retry_attempts=expected_attempts,
        missing_task_checks=None,
    )

    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    [
        "unexpected-status",
    ],
)
async def test_unsupported_status_propagates_error(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
    status: str | None,
) -> None:
    """Do not acknowledge or republish unsupported task states."""
    error = handler_module.RetryableMessageError(
        task_id="task-001",
        status=status,
    )

    handle = AsyncMock(side_effect=error)
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    with pytest.raises(handler_module.RetryableMessageError) as exc_info:
        await handler_module.handle_investigation_message_with_retry(
            message=incoming_message,
            exchange=Mock(),
            processor=AsyncMock(),
        )

    assert exc_info.value is error

    republish.assert_not_awaited()
    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_attempts",
    [
        -1,
        "2",
        True,
        1.5,
        None,
    ],
)
async def test_invalid_retry_header_is_rejected(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
    invalid_attempts: object,
) -> None:
    """Reject malformed retry metadata before publishing."""
    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: invalid_attempts,
    }

    error = handler_module.RetryableMessageError(
        task_id="task-001",
        status="running",
    )

    handle = AsyncMock(side_effect=error)
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    with pytest.raises(
        ValueError,
        match="Invalid retry attempts header",
    ) as exc_info:
        await handler_module.handle_investigation_message_with_retry(
            message=incoming_message,
            exchange=Mock(),
            processor=AsyncMock(),
        )

    assert exc_info.value.__cause__ is None
    handle.assert_not_awaited()

    republish.assert_not_awaited()
    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
async def test_republish_failure_propagates(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagate publication failures without acknowledging the message."""
    handle = AsyncMock(
        side_effect=handler_module.RetryableMessageError(
            task_id="task-001",
            status="running",
        )
    )

    publish_error = RuntimeError("Publisher confirm failed")

    republish = AsyncMock(side_effect=publish_error)

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await handler_module.handle_investigation_message_with_retry(
            message=incoming_message,
            exchange=Mock(),
            processor=AsyncMock(),
        )

    assert exc_info.value is publish_error

    republish.assert_awaited_once()
    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("previous_checks", "expected_routing_key", "expected_checks"),
    [
        pytest.param(
            0,
            RETRY_ROUTING_KEY,
            1,
            id="first-missing-check",
        ),
        pytest.param(
            1,
            RETRY_ROUTING_KEY,
            2,
            id="second-missing-check",
        ),
        pytest.param(
            2,
            DEAD_ROUTING_KEY,
            3,
            id="third-missing-check",
        ),
    ],
)
async def test_missing_task_retries_then_dead_letters(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
    previous_checks: int,
    expected_routing_key: str,
    expected_checks: int,
) -> None:
    """Route a persistently missing task to the dead-letter queue."""
    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: 2,
    }

    if previous_checks > 0:
        incoming_message.headers[MISSING_TASK_CHECKS_HEADER] = previous_checks

    original_headers = dict(incoming_message.headers)

    error = handler_module.RetryableMessageError(
        task_id="task-001",
        status=None,
    )

    handle = AsyncMock(side_effect=error)
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    exchange = Mock()

    await handler_module.handle_investigation_message_with_retry(
        message=incoming_message,
        exchange=exchange,
        processor=AsyncMock(),
    )

    republish.assert_awaited_once_with(
        exchange=exchange,
        incoming_message=incoming_message,
        routing_key=expected_routing_key,
        retry_attempts=2,
        missing_task_checks=expected_checks,
    )

    # Republishing must not modify the original message.
    assert incoming_message.headers == original_headers

    # The handler must not acknowledge the original message.
    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_checks",
    [
        -1,
        "2",
        True,
        1.5,
        None,
    ],
)
async def test_invalid_missing_task_checks_header_is_rejected(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
    invalid_checks: object,
) -> None:
    """Reject malformed missing-task metadata before republishing."""
    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: 0,
        MISSING_TASK_CHECKS_HEADER: invalid_checks,
    }

    error = handler_module.RetryableMessageError(
        task_id="task-001",
        status=None,
    )

    handle = AsyncMock(side_effect=error)
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    with pytest.raises(
        ValueError,
        match="Invalid missing task checks header",
    ) as exc_info:
        await handler_module.handle_investigation_message_with_retry(
            message=incoming_message,
            exchange=Mock(),
            processor=AsyncMock(),
        )

    assert exc_info.value.__cause__ is error

    republish.assert_not_awaited()
    incoming_message.ack.assert_not_awaited()


@pytest.fixture
def retryable_failure_mocks(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncMock, AsyncMock, Exception]:
    """Mock a retryable investigation failure and message publication."""
    error = handler_module.RetryableInvestigationError("GitHub API timeout")

    handle = AsyncMock(side_effect=error)
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    return handle, republish, error


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("previous_attempts", "expected_attempts"),
    [
        pytest.param(None, 1, id="missing-retry-header"),
        pytest.param(0, 1, id="first-failure"),
        pytest.param(1, 2, id="second-failure"),
        pytest.param(2, 3, id="third-failure"),
    ],
)
async def test_retryable_failure_increments_attempts(
    incoming_message: Mock,
    retryable_failure_mocks: tuple[AsyncMock, AsyncMock, Exception],
    previous_attempts: int | None,
    expected_attempts: int,
) -> None:
    """Increase retry attempts after a retryable investigation failure."""
    _, republish, _ = retryable_failure_mocks

    incoming_message.headers = {}

    if previous_attempts is not None:
        incoming_message.headers[RETRY_ATTEMPTS_HEADER] = previous_attempts

    original_headers = dict(incoming_message.headers)
    exchange = Mock()

    await handler_module.handle_investigation_message_with_retry(
        message=incoming_message,
        exchange=exchange,
        processor=AsyncMock(),
    )

    republish.assert_awaited_once_with(
        exchange=exchange,
        incoming_message=incoming_message,
        routing_key=RETRY_ROUTING_KEY,
        retry_attempts=expected_attempts,
        missing_task_checks=None,
    )

    assert incoming_message.headers == original_headers
    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("retry_attempts", [3, 4])
async def test_retry_limit_dead_letters_message(
    incoming_message: Mock,
    retryable_failure_mocks: tuple[AsyncMock, AsyncMock, Exception],
    retry_attempts: int,
) -> None:
    """Dead-letter an investigation when retry attempts are exhausted."""
    _, republish, _ = retryable_failure_mocks

    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: retry_attempts,
    }

    exchange = Mock()

    await handler_module.handle_investigation_message_with_retry(
        message=incoming_message,
        exchange=exchange,
        processor=AsyncMock(),
    )

    republish.assert_awaited_once_with(
        exchange=exchange,
        incoming_message=incoming_message,
        routing_key=handler_module.DEAD_ROUTING_KEY,
        retry_attempts=retry_attempts,
        missing_task_checks=None,
    )

    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "invalid_attempts",
    [
        -1,
        "2",
        True,
        1.5,
        None,
    ],
)
async def test_retryable_failure_rejects_invalid_attempts(
    incoming_message: Mock,
    retryable_failure_mocks: tuple[AsyncMock, AsyncMock, Exception],
    invalid_attempts: object,
) -> None:
    """Reject invalid retry metadata after an investigation failure."""
    _, republish, _ = retryable_failure_mocks

    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: invalid_attempts,
    }

    with pytest.raises(
        ValueError,
        match="Invalid retry attempts header",
    ):
        await handler_module.handle_investigation_message_with_retry(
            message=incoming_message,
            exchange=Mock(),
            processor=AsyncMock(),
        )

    republish.assert_not_awaited()
    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
async def test_retryable_failure_publish_error_propagates(
    incoming_message: Mock,
    retryable_failure_mocks: tuple[AsyncMock, AsyncMock, Exception],
) -> None:
    """Propagate retry publication failures without acknowledging."""
    _, republish, _ = retryable_failure_mocks

    publish_error = RuntimeError("Publisher confirm failed")
    republish.side_effect = publish_error

    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: 1,
    }

    with pytest.raises(RuntimeError) as exc_info:
        await handler_module.handle_investigation_message_with_retry(
            message=incoming_message,
            exchange=Mock(),
            processor=AsyncMock(),
        )

    assert exc_info.value is publish_error

    republish.assert_awaited_once()
    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("retry_attempts", "expected_final"),
    [
        (0, False),
        (1, False),
        (2, False),
        (3, True),
    ],
)
async def test_final_attempt_forwarded_to_processor(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
    retry_attempts: int,
    expected_final: bool,
) -> None:
    """Pass the final-attempt flag to the investigation processor."""
    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: retry_attempts,
    }

    request = handler_module.InvestigationRequestedMessage(
        task_id="task-001",
        repository="fastapi/fastapi",
        issue_number=123,
    )

    async def fake_handle(message: Mock, processor: object) -> None:
        await processor(request)

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        fake_handle,
    )

    processor = AsyncMock()

    await handler_module.handle_investigation_message_with_retry(
        message=incoming_message,
        exchange=Mock(),
        processor=processor,
    )

    processor.assert_awaited_once_with(
        request,
        expected_final,
    )


@pytest.mark.anyio
async def test_final_failure_goes_to_dead_queue(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dead-letter an investigation after its final failed execution."""
    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: 3,
    }

    handle = AsyncMock(
        side_effect=handler_module.RetryableInvestigationError("GitHub API timeout")
    )
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    exchange = Mock()

    await handler_module.handle_investigation_message_with_retry(
        message=incoming_message,
        exchange=exchange,
        processor=AsyncMock(),
    )

    republish.assert_awaited_once_with(
        exchange=exchange,
        incoming_message=incoming_message,
        routing_key=DEAD_ROUTING_KEY,
        retry_attempts=3,
        missing_task_checks=None,
    )

    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
async def test_failed_task_is_dead_lettered_on_redelivery(
    incoming_message: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry dead-letter publication when the database task is failed."""
    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: 3,
    }

    handle = AsyncMock(
        side_effect=handler_module.RetryableMessageError(
            task_id="task-001",
            status="failed",
        )
    )
    republish = AsyncMock()

    monkeypatch.setattr(
        handler_module,
        "handle_investigation_message",
        handle,
    )
    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    exchange = Mock()

    await handler_module.handle_investigation_message_with_retry(
        message=incoming_message,
        exchange=exchange,
        processor=AsyncMock(),
    )

    republish.assert_awaited_once_with(
        exchange=exchange,
        incoming_message=incoming_message,
        routing_key=DEAD_ROUTING_KEY,
        retry_attempts=3,
        missing_task_checks=None,
    )

    incoming_message.ack.assert_not_awaited()
