"""Unit tests for FastAPI application lifespan management."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from maintainer_lens.main import app


def test_lifespan_initializes_and_releases_resources() -> None:
    """Verify that application resources are initialized and released."""
    database_url = "postgresql+asyncpg://test:password@localhost:5432/test_db"
    redis_url = "redis://:password@localhost:6379/0"

    settings = SimpleNamespace(
        database_url=SecretStr(database_url),
        redis_url=SecretStr(redis_url),
    )

    mock_engine = Mock()
    mock_engine.dispose = AsyncMock()

    mock_redis_client = Mock()
    mock_redis_client.aclose = AsyncMock()

    mock_session_factory = Mock()

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
    ):
        with TestClient(app) as client:
            assert client.get("/health/live").status_code == 200

            assert app.state.session_factory is mock_session_factory
            assert app.state.redis_client is mock_redis_client

            mock_engine.dispose.assert_not_awaited()
            mock_redis_client.aclose.assert_not_awaited()

        mock_get_settings.assert_called_once_with()
        mock_create_engine.assert_called_once_with(database_url)
        mock_create_session_factory.assert_called_once_with(mock_engine)
        mock_create_redis_client.assert_called_once_with(redis_url)

        mock_engine.dispose.assert_awaited_once_with()
        mock_redis_client.aclose.assert_awaited_once_with()


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
