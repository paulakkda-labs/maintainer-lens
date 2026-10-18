"""Tests for the investigation worker startup lifecycle."""

from unittest.mock import AsyncMock, MagicMock, Mock, call

import pytest

import maintainer_lens.worker as worker


@pytest.mark.anyio
@pytest.mark.parametrize("consumer_fails", [False, True])
async def test_run_worker_manages_resources(
    monkeypatch: pytest.MonkeyPatch,
    consumer_fails: bool,
) -> None:
    """Initialize worker dependencies and close resources on exit."""
    settings = MagicMock()
    settings.database_url.get_secret_value.return_value = "postgres-url"
    settings.rabbitmq_url.get_secret_value.return_value = "rabbitmq-url"

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
    investigate = AsyncMock()

    create_engine = Mock(return_value=engine)
    create_factory = Mock(return_value=session_factory)
    connect = AsyncMock(return_value=connection)
    declare = AsyncMock(return_value=exchange)
    consume = AsyncMock()

    error = RuntimeError("Consumer failed")

    if consumer_fails:
        consume.side_effect = error

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "create_db_engine", create_engine)
    monkeypatch.setattr(worker, "create_session_factory", create_factory)
    monkeypatch.setattr(worker, "create_rabbitmq_connection", connect)
    monkeypatch.setattr(worker, "declare_investigation_topology", declare)
    monkeypatch.setattr(worker, "run_investigation_consumer", consume)

    if consumer_fails:
        with pytest.raises(RuntimeError) as exc_info:
            await worker.run_worker(investigate=investigate)

        assert exc_info.value is error
    else:
        await worker.run_worker(investigate=investigate)

    create_engine.assert_called_once_with("postgres-url")
    create_factory.assert_called_once_with(engine)
    connect.assert_awaited_once_with("rabbitmq-url")

    connection.channel.assert_has_awaits(
        [
            call(publisher_confirms=False),
            call(
                publisher_confirms=True,
                on_return_raises=True,
            ),
        ]
    )

    declare.assert_awaited_once_with(publisher_channel)

    consumer_channel.get_queue.assert_awaited_once_with(
        worker.QUEUE_NAME,
        ensure=True,
    )
    consumer_channel.set_qos.assert_awaited_once_with(prefetch_count=1)

    consume.assert_awaited_once_with(
        queue=queue,
        exchange=exchange,
        session_factory=session_factory,
        investigate=investigate,
    )

    publisher_channel.close.assert_awaited_once()
    consumer_channel.close.assert_awaited_once()
    connection.close.assert_awaited_once()
    engine.dispose.assert_awaited_once()
