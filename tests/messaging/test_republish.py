"""Tests for investigation message republishing."""

from unittest.mock import AsyncMock, Mock

import aio_pika
import pytest

from maintainer_lens.messaging.publisher import republish_investigation_message
from maintainer_lens.messaging.schemas import RETRY_ATTEMPTS_HEADER
from maintainer_lens.messaging.topology import (
    DEAD_ROUTING_KEY,
    RETRY_ROUTING_KEY,
)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("routing_key", "retry_attempts"),
    [
        pytest.param(RETRY_ROUTING_KEY, 1, id="retry"),
        pytest.param(DEAD_ROUTING_KEY, 3, id="dead"),
    ],
)
async def test_republish_preserves_message_and_updates_headers(
    routing_key: str,
    retry_attempts: int,
) -> None:
    """Preserve message identity while setting retry metadata."""
    incoming_message = Mock()
    incoming_message.body = b'{"task_id":"task-001"}'
    incoming_message.content_type = "application/json"
    incoming_message.message_id = "task-001"
    incoming_message.headers = {
        "x-death": [{"reason": "expired"}],
        RETRY_ATTEMPTS_HEADER: 0,
    }
    incoming_message.ack = AsyncMock()

    exchange = Mock()
    exchange.publish = AsyncMock()

    await republish_investigation_message(
        exchange=exchange,
        incoming_message=incoming_message,
        routing_key=routing_key,
        retry_attempts=retry_attempts,
    )

    exchange.publish.assert_awaited_once()

    publish_kwargs = exchange.publish.await_args.kwargs

    assert publish_kwargs["routing_key"] == routing_key
    assert publish_kwargs["mandatory"] is True

    published_message = publish_kwargs["message"]

    assert isinstance(published_message, aio_pika.Message)
    assert published_message.body == incoming_message.body
    assert published_message.content_type == "application/json"
    assert published_message.message_id == "task-001"

    assert published_message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT

    assert published_message.headers == {
        RETRY_ATTEMPTS_HEADER: retry_attempts,
    }

    # The original message must not be modified.
    assert incoming_message.headers[RETRY_ATTEMPTS_HEADER] == 0

    # Broker-generated dead-letter headers must not be copied.
    assert "x-death" not in published_message.headers

    # The publisher must never acknowledge the incoming message.
    incoming_message.ack.assert_not_awaited()


@pytest.mark.anyio
async def test_republish_propagates_publish_failure() -> None:
    """Propagate publication failures without acknowledging the original."""
    incoming_message = Mock()
    incoming_message.body = b'{"task_id":"task-001"}'
    incoming_message.content_type = "application/json"
    incoming_message.message_id = "task-001"
    incoming_message.ack = AsyncMock()

    exchange = Mock()
    exchange.publish = AsyncMock(side_effect=RuntimeError("Publisher confirm failed"))

    with pytest.raises(
        RuntimeError,
        match="Publisher confirm failed",
    ):
        await republish_investigation_message(
            exchange=exchange,
            incoming_message=incoming_message,
            routing_key=RETRY_ROUTING_KEY,
            retry_attempts=1,
        )

    exchange.publish.assert_awaited_once()

    # A failed republish must never acknowledge the original message.
    incoming_message.ack.assert_not_awaited()
