"""Unit tests for FastAPI application lifespan management."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import aio_pika
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from maintainer_lens.main import app


def test_lifespan_initializes_and_releases_resources() -> None:
    """Verify that application resources are initialized and released."""
    database_url = "postgresql+asyncpg://test:password@localhost:5432/test_db"
    redis_url = "redis://:password@localhost:6379/0"
    rabbitmq_url = "amqp://guest:guest@localhost:5672/test"

    settings = SimpleNamespace(
        database_url=SecretStr(database_url),
        redis_url=SecretStr(redis_url),
        rabbitmq_url=SecretStr(rabbitmq_url),
    )

    mock_engine = Mock()
    mock_engine.dispose = AsyncMock()

    mock_redis_client = Mock()
    mock_redis_client.aclose = AsyncMock()

    mock_session_factory = Mock()

    mock_channel = Mock()
    mock_channel.close = AsyncMock()

    mock_exchange = Mock()

    mock_queue = Mock()
    mock_queue.bind = AsyncMock()

    mock_channel.declare_exchange = AsyncMock(return_value=mock_exchange)
    mock_channel.declare_queue = AsyncMock(return_value=mock_queue)

    mock_connection = Mock()
    mock_connection.channel = AsyncMock(return_value=mock_channel)
    mock_connection.close = AsyncMock()

    with (
        patch(
            "maintainer_lens.main.get_settings",
            return_value=settings,
        ) as mock_get_settings,
        patch(
            "maintainer_lens.main.create_db_engine",
            return_value=mock_engine,
        ) as mock_create_engine,
        patch(
            "maintainer_lens.main.create_session_factory",
            return_value=mock_session_factory,
        ) as mock_create_session_factory,
        patch(
            "maintainer_lens.main.create_redis_client",
            return_value=mock_redis_client,
        ) as mock_create_redis_client,
        patch(
            "maintainer_lens.main.create_rabbitmq_connection",
            new_callable=AsyncMock,
            return_value=mock_connection,
        ) as mock_create_rabbitmq_connection,
    ):
        with TestClient(app) as client:
            assert client.get("/health/live").status_code == 200

            assert app.state.session_factory is mock_session_factory
            assert app.state.redis_client is mock_redis_client

            mock_engine.dispose.assert_not_awaited()
            mock_redis_client.aclose.assert_not_awaited()

            assert app.state.rabbitmq_channel is mock_channel
            assert app.state.rabbitmq_exchange is mock_exchange

            mock_channel.close.assert_not_awaited()
            mock_connection.close.assert_not_awaited()

        mock_get_settings.assert_called_once_with()
        mock_create_engine.assert_called_once_with(database_url)
        mock_create_session_factory.assert_called_once_with(mock_engine)
        mock_create_redis_client.assert_called_once_with(redis_url)

        mock_engine.dispose.assert_awaited_once_with()
        mock_redis_client.aclose.assert_awaited_once_with()

        mock_create_rabbitmq_connection.assert_awaited_once_with(rabbitmq_url)

        mock_connection.channel.assert_awaited_once_with(
            publisher_confirms=True,
            on_return_raises=True,
        )

        mock_channel.close.assert_awaited_once_with()
        mock_connection.close.assert_awaited_once_with()

        mock_channel.declare_exchange.assert_awaited_once_with(
            "maintainer_lens.investigations",
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
        )

        mock_channel.declare_queue.assert_has_awaits(
            [
                call(
                    "maintainer_lens.investigations.pending",
                    durable=True,
                ),
                call(
                    "maintainer_lens.investigations.retry",
                    durable=True,
                    arguments={
                        "x-message-ttl": 60_000,
                        "x-dead-letter-exchange": "maintainer_lens.investigations",
                        "x-dead-letter-routing-key": "investigation.requested",
                    },
                ),
                call(
                    "maintainer_lens.investigations.dead",
                    durable=True,
                ),
            ]
        )

        assert mock_channel.declare_queue.await_count == 3

        mock_queue.bind.assert_has_awaits(
            [
                call(
                    mock_exchange,
                    routing_key="investigation.requested",
                ),
                call(
                    mock_exchange,
                    routing_key="investigation.retry",
                ),
                call(
                    mock_exchange,
                    routing_key="investigation.dead",
                ),
            ]
        )

        assert mock_queue.bind.await_count == 3


def test_lifespan_releases_engine_when_redis_initialization_fails() -> None:
    """Verify that database resources are released after startup failure."""
    database_url = "postgresql+asyncpg://test:password@localhost:5432/test_db"
    redis_url = "redis://:password@localhost:6379/0"

    settings = SimpleNamespace(
        database_url=SecretStr(database_url),
        redis_url=SecretStr(redis_url),
    )

    mock_engine = Mock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("maintainer_lens.main.get_settings", return_value=settings),
        patch("maintainer_lens.main.create_db_engine", return_value=mock_engine),
        patch("maintainer_lens.main.create_session_factory"),
        patch(
            "maintainer_lens.main.create_redis_client",
            side_effect=RuntimeError("Redis initialization failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="Redis initialization failed"):
            with TestClient(app):
                pass

    mock_engine.dispose.assert_awaited_once_with()


def test_lifespan_releases_resources_when_rabbitmq_channel_fails() -> None:
    """Release initialized resources when RabbitMQ channel creation fails."""
    settings = SimpleNamespace(
        database_url=SecretStr(
            "postgresql+asyncpg://test:password@localhost:5432/test_db"
        ),
        redis_url=SecretStr("redis://:password@localhost:6379/0"),
        rabbitmq_url=SecretStr("amqp://guest:guest@localhost:5672/test"),
    )

    mock_engine = Mock()
    mock_engine.dispose = AsyncMock()

    mock_redis_client = Mock()
    mock_redis_client.aclose = AsyncMock()

    mock_connection = Mock()
    mock_connection.channel = AsyncMock(
        side_effect=RuntimeError("Channel initialization failed")
    )
    mock_connection.close = AsyncMock()

    with (
        patch("maintainer_lens.main.get_settings", return_value=settings),
        patch("maintainer_lens.main.create_db_engine", return_value=mock_engine),
        patch("maintainer_lens.main.create_session_factory"),
        patch(
            "maintainer_lens.main.create_redis_client",
            return_value=mock_redis_client,
        ),
        patch(
            "maintainer_lens.main.create_rabbitmq_connection",
            new_callable=AsyncMock,
            return_value=mock_connection,
        ),
    ):
        with pytest.raises(RuntimeError, match="Channel initialization failed"):
            with TestClient(app):
                pass

    mock_connection.channel.assert_awaited_once_with(
        publisher_confirms=True,
        on_return_raises=True,
    )

    mock_connection.close.assert_awaited_once_with()
    mock_redis_client.aclose.assert_awaited_once_with()
    mock_engine.dispose.assert_awaited_once_with()


@pytest.mark.anyio
async def test_lifespan_cleans_up_when_topology_declaration_fails():
    """Close initialized resources when RabbitMQ topology declaration fails."""
    engine = MagicMock()
    engine.dispose = AsyncMock()

    redis_client = MagicMock()
    redis_client.aclose = AsyncMock()

    rabbitmq_connection = MagicMock()
    rabbitmq_connection.close = AsyncMock()

    rabbitmq_channel = MagicMock()
    rabbitmq_channel.close = AsyncMock()

    rabbitmq_connection.channel = AsyncMock(return_value=rabbitmq_channel)
    settings = SimpleNamespace(
        database_url=SecretStr(
            "postgresql+asyncpg://test:password@localhost:5432/test_db"
        ),
        redis_url=SecretStr("redis://:password@localhost:6379/0"),
        rabbitmq_url=SecretStr("amqp://guest:guest@localhost:5672/test"),
    )

    with (
        patch(
            "maintainer_lens.main.get_settings",
            return_value=settings,
        ),
        patch(
            "maintainer_lens.main.create_db_engine",
            return_value=engine,
        ),
        patch(
            "maintainer_lens.main.create_session_factory",
            return_value=MagicMock(),
        ),
        patch(
            "maintainer_lens.main.create_redis_client",
            return_value=redis_client,
        ),
        patch(
            "maintainer_lens.main.create_rabbitmq_connection",
            new_callable=AsyncMock,
            return_value=rabbitmq_connection,
        ),
        patch(
            "maintainer_lens.main.declare_investigation_topology",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Topology declaration failed"),
        ),
    ):
        with pytest.raises(RuntimeError, match="Topology declaration failed"):
            async with app.router.lifespan_context(app):
                pass

    rabbitmq_connection.channel.assert_awaited_once_with(
        publisher_confirms=True,
        on_return_raises=True,
    )
    rabbitmq_channel.close.assert_awaited_once()
    rabbitmq_connection.close.assert_awaited_once()
    redis_client.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()
