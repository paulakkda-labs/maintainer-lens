"""Unit tests for RabbitMQ investigation message consumption."""

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from maintainer_lens.messaging.consumer import consume_investigations


def make_mock_queue(*messages: Mock) -> Mock:
    """Create a mock queue that yields the provided messages."""
    queue = Mock()
    iterator = MagicMock()

    iterator.__aenter__ = AsyncMock(return_value=iterator)
    iterator.__aexit__ = AsyncMock(return_value=None)
    iterator.__aiter__.return_value = iter(messages)

    queue.iterator.return_value = iterator

    return queue


@pytest.mark.anyio
async def test_consumer_acknowledges_after_successful_processing() -> None:
    """Acknowledge a message only after its handler succeeds."""
    events = []

    message = Mock()

    async def acknowledge() -> None:
        events.append("ack")

    message.ack = AsyncMock(side_effect=acknowledge)

    async def handler(received: Mock) -> None:
        assert received is message
        events.append("handled")

    queue = make_mock_queue(message)

    await consume_investigations(queue, handler)

    assert events == ["handled", "ack"]
    message.ack.assert_awaited_once_with()
    queue.iterator.assert_called_once_with()


@pytest.mark.anyio
async def test_consumer_does_not_acknowledge_failed_processing() -> None:
    """Propagate handler failures without acknowledging the message."""
    message = Mock()
    message.ack = AsyncMock()

    handler = AsyncMock(side_effect=RuntimeError("Investigation processing failed"))
    queue = make_mock_queue(message)

    with pytest.raises(RuntimeError, match="Investigation processing failed"):
        await consume_investigations(queue, handler)

    handler.assert_awaited_once_with(message)
    message.ack.assert_not_awaited()
