"""Tests for production investigation worker wiring."""

from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
from pydantic import SecretStr

import maintainer_lens.worker as worker
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage


@pytest.mark.anyio
async def test_worker_wires_production_investigator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wire the real investigation service into the worker."""

    settings = MagicMock()
    settings.database_url.get_secret_value.return_value = "postgres-url"
    settings.rabbitmq_url.get_secret_value.return_value = "rabbitmq-url"
    settings.github_token = SecretStr("fake-test-token")

    engine = MagicMock()
    engine.dispose = AsyncMock()

    session_factory = Mock()

    connection = MagicMock()
    connection.close = AsyncMock()

    consumer_channel = MagicMock()
    consumer_channel.close = AsyncMock()
    consumer_channel.set_qos = AsyncMock()

    queue = Mock()
    consumer_channel.get_queue = AsyncMock(return_value=queue)

    publisher_channel = MagicMock()
    publisher_channel.close = AsyncMock()

    connection.channel = AsyncMock(side_effect=[consumer_channel, publisher_channel])

    exchange = Mock()

    request = InvestigationRequestedMessage(
        task_id="task-001",
        repository="encode/httpx",
        issue_number=3712,
    )

    investigate_service = AsyncMock()

    async def fake_consumer(
        *,
        queue,
        exchange,
        session_factory,
        investigate,
    ) -> None:
        await investigate(request, "claim-token-001")

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(
        worker,
        "create_db_engine",
        Mock(return_value=engine),
    )
    monkeypatch.setattr(
        worker,
        "create_session_factory",
        Mock(return_value=session_factory),
    )
    monkeypatch.setattr(
        worker,
        "create_rabbitmq_connection",
        AsyncMock(return_value=connection),
    )
    monkeypatch.setattr(
        worker,
        "declare_investigation_topology",
        AsyncMock(return_value=exchange),
    )
    monkeypatch.setattr(
        worker,
        "run_investigation_consumer",
        fake_consumer,
    )
    monkeypatch.setattr(
        worker,
        "investigate_issue",
        investigate_service,
    )

    await worker.run_worker()

    investigate_service.assert_awaited_once()

    kwargs = investigate_service.await_args.kwargs

    assert kwargs["session_factory"] is session_factory
    assert kwargs["task_id"] == "task-001"
    assert kwargs["claim_token"] == "claim-token-001"
    assert kwargs["repository"] == "encode/httpx"
    assert kwargs["issue_number"] == 3712

    http_client = kwargs["http_client"]

    assert isinstance(http_client, httpx.AsyncClient)
    assert http_client.is_closed
    assert http_client.headers["Authorization"] == "Bearer fake-test-token"

    engine.dispose.assert_awaited_once()
    connection.close.assert_awaited_once()
