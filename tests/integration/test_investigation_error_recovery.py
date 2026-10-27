"""Verify processor error routing with real task transactions and fake MQ.

No external provider or broker is contacted. Each task is isolated and cleaned
up by the shared PostgreSQL fixtures.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import update

from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.investigation_runner import (
    LeaseLostError,
    RetryableInvestigationError,
    execute_investigation_task,
)
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.messaging.topology import DEAD_ROUTING_KEY, RETRY_ROUTING_KEY
from maintainer_lens.worker import build_investigation_message_handler

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def processor_error(kind):
    if kind == "timeout":
        return TimeoutError("Upstream deadline")
    if kind == "unknown":
        return RuntimeError("Unexpected processor failure")
    if kind == "invalid":
        return ValueError("Invalid provider output")
    if kind == "group":
        return ExceptionGroup(
            "parallel retrieval",
            [
                ExceptionGroup("nested", [TimeoutError("Upstream deadline")]),
                RetryableInvestigationError("Temporary retrieval failure"),
            ],
        )
    response = httpx.Response(
        int(kind), request=httpx.Request("POST", "https://example.test")
    )
    return httpx.HTTPStatusError(
        "Upstream failure", request=response.request, response=response
    )


@pytest.mark.parametrize(
    ("kind", "attempts", "expected_status", "routing_key", "next_attempts"),
    [
        ("timeout", 0, "pending", RETRY_ROUTING_KEY, 1),
        ("unknown", 0, "pending", RETRY_ROUTING_KEY, 1),
        ("group", 0, "pending", RETRY_ROUTING_KEY, 1),
        ("429", 0, "pending", RETRY_ROUTING_KEY, 1),
        ("503", 0, "pending", RETRY_ROUTING_KEY, 1),
        ("401", 0, "failed", DEAD_ROUTING_KEY, 0),
        ("invalid", 0, "failed", DEAD_ROUTING_KEY, 0),
        ("timeout", 3, "failed", DEAD_ROUTING_KEY, 3),
    ],
)
async def test_processor_failure_commits_state_before_confirmed_republish(
    investigation_task,
    kind,
    attempts,
    expected_status,
    routing_key,
    next_attempts,
):
    factory, task_id, _ = investigation_task
    request = InvestigationRequestedMessage(
        task_id=task_id,
        repository="fastapi/fastapi",
        issue_number=123,
    )
    message = SimpleNamespace(
        body=request.model_dump_json().encode(),
        headers={"ml-retry-attempts": attempts},
        content_type="application/json",
        message_id=task_id,
    )

    async def publish(*, message, routing_key, mandatory):
        assert mandatory is True
        async with factory() as session:
            task = await session.get(InvestigationTask, task_id)
            assert task.status == expected_status
            assert task.claim_token is None
            assert task.lease_expires_at is None

    exchange = AsyncMock()
    exchange.publish.side_effect = publish

    async def investigate(request, token):
        assert token
        raise processor_error(kind)

    handler = build_investigation_message_handler(factory, exchange, investigate)
    await handler(message)
    exchange.publish.assert_awaited_once()
    sent = exchange.publish.await_args.kwargs
    assert sent["routing_key"] == routing_key
    assert sent["message"].headers["ml-retry-attempts"] == next_attempts


async def test_lost_owner_cannot_release_replacement_claim(investigation_task):
    factory, task_id, _ = investigation_task

    async def investigate(request, token):
        async with factory.begin() as session:
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(claim_token="replacement-owner")
            )
        raise LeaseLostError("Old processor lost ownership")

    request = InvestigationRequestedMessage(
        task_id=task_id,
        repository="fastapi/fastapi",
        issue_number=123,
    )
    message = SimpleNamespace(
        body=request.model_dump_json().encode(),
        headers={},
        content_type="application/json",
        message_id=task_id,
    )
    exchange = AsyncMock()
    await build_investigation_message_handler(factory, exchange, investigate)(message)
    assert exchange.publish.await_args.kwargs["routing_key"] == RETRY_ROUTING_KEY
    assert (
        exchange.publish.await_args.kwargs["message"].headers["ml-retry-attempts"] == 0
    )
    async with factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert task.status == "running"
        assert task.claim_token == "replacement-owner"


async def test_external_cancellation_never_completes_task(investigation_task):
    factory, task_id, _ = investigation_task
    started = asyncio.Event()

    async def processor(token):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(execute_investigation_task(factory, task_id, processor))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with factory() as session:
        stored = await session.get(InvestigationTask, task_id)
        assert stored.status == "running"
        assert stored.claim_token is not None
