"""Regression coverage for poison deliveries and lease-loss routing."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import maintainer_lens.messaging.handler as handler_module
from maintainer_lens.investigation_runner import LeaseLostError
from maintainer_lens.messaging.consumer import consume_investigations
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.messaging.topology import DEAD_ROUTING_KEY

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class OneMessageQueue:
    def __init__(self, message):
        self.message = message

    @asynccontextmanager
    async def iterator(self):
        async def messages():
            yield self.message

        yield messages()


@pytest.mark.parametrize(
    "headers",
    [
        {"ml-retry-attempts": True},
        {"ml-missing-task-checks": -1},
        {"ml-retry-not-before": "not-a-date"},
        {"ml-retry-not-before": "2026-10-28T12:00:00"},
        {},
    ],
)
@pytest.mark.parametrize("publish_fails", [False, True])
async def test_poison_message_ack_requires_dead_letter_confirmation(
    headers, publish_fails
):
    message = SimpleNamespace(
        body=b"invalid JSON",
        headers=headers,
        message_id="poison-message",
        content_type="application/json",
        ack=AsyncMock(),
    )
    exchange = SimpleNamespace(publish=AsyncMock())
    if publish_fails:
        exchange.publish.side_effect = RuntimeError("Publisher confirm failed")
    processor = AsyncMock()

    async def handle(delivery):
        await handler_module.handle_investigation_message_with_retry(
            delivery, exchange, processor
        )

    if publish_fails:
        with pytest.raises(RuntimeError, match="Publisher confirm failed"):
            await consume_investigations(OneMessageQueue(message), handle)
        message.ack.assert_not_awaited()
    else:
        await consume_investigations(OneMessageQueue(message), handle)
        message.ack.assert_awaited_once()
    assert exchange.publish.await_args.kwargs["routing_key"] == DEAD_ROUTING_KEY
    processor.assert_not_awaited()


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("status", ["pending", "running", "failed", "succeeded", None])
async def test_lease_loss_rechecks_authoritative_status(monkeypatch, grouped, status):
    error = LeaseLostError("Lease no longer valid")
    if grouped:
        error = ExceptionGroup("outer", [ExceptionGroup("inner", [error])])
    monkeypatch.setattr(
        handler_module, "execute_investigation_task", AsyncMock(side_effect=error)
    )
    monkeypatch.setattr(
        handler_module, "get_investigation_task_status", AsyncMock(return_value=status)
    )
    request = InvestigationRequestedMessage(
        task_id="task", repository="org/repo", issue_number=1
    )
    operation = handler_module.process_investigation_request(
        request, MagicMock(), AsyncMock()
    )
    if status == "succeeded":
        await operation
    else:
        with pytest.raises(handler_module.RetryableMessageError) as captured:
            await operation
        assert captured.value.status == status
        assert captured.value.__cause__ is error


async def test_mixed_lease_error_group_is_not_misclassified(monkeypatch):
    error = ExceptionGroup(
        "mixed", [LeaseLostError("lost"), RuntimeError("DB unavailable")]
    )
    monkeypatch.setattr(
        handler_module, "execute_investigation_task", AsyncMock(side_effect=error)
    )
    lookup = AsyncMock()
    monkeypatch.setattr(handler_module, "get_investigation_task_status", lookup)
    request = InvestigationRequestedMessage(
        task_id="task", repository="org/repo", issue_number=1
    )
    with pytest.raises(ExceptionGroup) as captured:
        await handler_module.process_investigation_request(
            request, MagicMock(), AsyncMock()
        )
    assert captured.value is error
    lookup.assert_not_awaited()
