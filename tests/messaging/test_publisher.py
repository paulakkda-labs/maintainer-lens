"""Unit tests for RabbitMQ investigation message publishing."""

import json
from unittest.mock import AsyncMock, Mock

import aio_pika
import pytest
from pydantic import ValidationError

from maintainer_lens.messaging.publisher import publish_investigation
from maintainer_lens.messaging.topology import ROUTING_KEY


@pytest.mark.anyio
async def test_publish_investigation_sends_persistent_message() -> None:
    """Publish a persistent JSON message with the correct routing key."""
    exchange = Mock()
    exchange.publish = AsyncMock()

    task_id = "test-task-001"
    repository = "fastapi/fastapi"
    issue_number = 123

    await publish_investigation(
        exchange=exchange,
        task_id=task_id,
        repository=repository,
        issue_number=issue_number,
    )

    exchange.publish.assert_awaited_once()

    publish_kwargs = exchange.publish.await_args.kwargs

    assert publish_kwargs["routing_key"] == ROUTING_KEY
    assert publish_kwargs["mandatory"] is True

    message = publish_kwargs["message"]

    assert isinstance(message, aio_pika.Message)
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert message.content_type == "application/json"
    assert message.message_id == task_id

    message_body = json.loads(message.body.decode("utf-8"))

    assert message_body == {
        "task_id": task_id,
        "repository": repository,
        "issue_number": issue_number,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("task_id", "repository", "issue_number"),
    [
        pytest.param("", "fastapi/fastapi", 123, id="empty-task-id"),
        pytest.param("task-001", "", 123, id="empty-repository"),
        pytest.param("task-001", "fastapi/fastapi", 0, id="zero-issue-number"),
        pytest.param("task-001", "fastapi/fastapi", -1, id="negative-issue-number"),
        pytest.param("task-001", "fastapi/fastapi", "123", id="string-issue-number"),
        pytest.param("task-001", "fastapi/fastapi", True, id="boolean-issue-number"),
    ],
)
async def test_publish_investigation_rejects_invalid_payload(
    task_id: str,
    repository: str,
    issue_number: int | str | bool,
) -> None:
    """Reject invalid investigation requests before publishing."""
    exchange = Mock()
    exchange.publish = AsyncMock()

    with pytest.raises(ValidationError):
        await publish_investigation(
            exchange=exchange,
            task_id=task_id,
            repository=repository,
            issue_number=issue_number,
        )

    exchange.publish.assert_not_awaited()
