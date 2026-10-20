"""Tests for investigation worker message wiring."""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import maintainer_lens.messaging.handler as handler_module
import maintainer_lens.worker as worker_module
from maintainer_lens.investigation_runner import RetryableInvestigationError
from maintainer_lens.messaging.schemas import (
    RETRY_ATTEMPTS_HEADER,
    InvestigationRequestedMessage,
)
from maintainer_lens.messaging.topology import (
    DEAD_ROUTING_KEY,
    RETRY_ROUTING_KEY,
)


@pytest.fixture
def investigation_request() -> InvestigationRequestedMessage:
    """Create a valid investigation request."""
    return InvestigationRequestedMessage(
        task_id="task-001",
        repository="fastapi/fastapi",
        issue_number=123,
    )


@pytest.fixture
def incoming_message(
    investigation_request: InvestigationRequestedMessage,
) -> Mock:
    """Create an incoming RabbitMQ message."""
    message = Mock()
    message.body = investigation_request.model_dump_json().encode()
    message.headers = {}
    message.ack = AsyncMock()
    return message


def single_message_queue(message: Mock) -> MagicMock:
    """Create a queue that delivers one message."""

    async def message_stream():
        yield message

    iterator = MagicMock()
    iterator.__aenter__ = AsyncMock(return_value=message_stream())
    iterator.__aexit__ = AsyncMock(return_value=False)

    queue = MagicMock()
    queue.iterator.return_value = iterator

    return queue


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("retry_attempts", "expected_final"),
    [
        (0, False),
        (3, True),
    ],
)
async def test_worker_passes_final_attempt(
    monkeypatch: pytest.MonkeyPatch,
    investigation_request: InvestigationRequestedMessage,
    incoming_message: Mock,
    retry_attempts: int,
    expected_final: bool,
) -> None:
    """Pass retry state and the investigation processor to the runner."""
    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: retry_attempts,
    }

    session_factory = MagicMock()
    investigate = AsyncMock()
    final_attempts: list[bool] = []

    async def fake_process_request(
        *,
        request,
        session_factory,
        processor,
        final_attempt,
    ) -> None:
        assert request == investigation_request
        assert session_factory is expected_factory

        final_attempts.append(final_attempt)
        await processor("test-claim-token")

    expected_factory = session_factory

    monkeypatch.setattr(
        worker_module,
        "process_investigation_request",
        fake_process_request,
    )

    await worker_module.run_investigation_consumer(
        queue=single_message_queue(incoming_message),
        exchange=Mock(),
        session_factory=session_factory,
        investigate=investigate,
    )

    assert final_attempts == [expected_final]

    investigate.assert_awaited_once_with(
        investigation_request,
        "test-claim-token",
    )
    incoming_message.ack.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("retry_attempts", "expected_route", "expected_attempts"),
    [
        (0, RETRY_ROUTING_KEY, 1),
        (3, DEAD_ROUTING_KEY, 3),
    ],
)
async def test_worker_acknowledges_after_republishing(
    monkeypatch: pytest.MonkeyPatch,
    incoming_message: Mock,
    retry_attempts: int,
    expected_route: str,
    expected_attempts: int,
) -> None:
    """Acknowledge the original message only after republishing."""
    incoming_message.headers = {
        RETRY_ATTEMPTS_HEADER: retry_attempts,
    }

    process = AsyncMock(side_effect=RetryableInvestigationError("GitHub API timeout"))

    monkeypatch.setattr(
        worker_module,
        "process_investigation_request",
        process,
    )

    async def fake_republish(**kwargs) -> None:
        incoming_message.ack.assert_not_awaited()

    republish = AsyncMock(side_effect=fake_republish)

    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    exchange = Mock()

    await worker_module.run_investigation_consumer(
        queue=single_message_queue(incoming_message),
        exchange=exchange,
        session_factory=MagicMock(),
        investigate=AsyncMock(),
    )

    republish.assert_awaited_once_with(
        exchange=exchange,
        incoming_message=incoming_message,
        routing_key=expected_route,
        retry_attempts=expected_attempts,
        missing_task_checks=None,
    )

    incoming_message.ack.assert_awaited_once()


@pytest.mark.anyio
async def test_worker_does_not_acknowledge_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
    incoming_message: Mock,
) -> None:
    """Do not acknowledge when retry publication fails."""
    process = AsyncMock(side_effect=RetryableInvestigationError("GitHub API timeout"))

    monkeypatch.setattr(
        worker_module,
        "process_investigation_request",
        process,
    )

    publish_error = RuntimeError("Publisher confirm failed")

    republish = AsyncMock(side_effect=publish_error)

    monkeypatch.setattr(
        handler_module,
        "republish_investigation_message",
        republish,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await worker_module.run_investigation_consumer(
            queue=single_message_queue(incoming_message),
            exchange=Mock(),
            session_factory=MagicMock(),
            investigate=AsyncMock(),
        )

    assert exc_info.value is publish_error

    republish.assert_awaited_once()
    incoming_message.ack.assert_not_awaited()
