"""Unit tests for FastAPI application lifespan management."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from pydantic import SecretStr

from maintainer_lens.main import app


def test_lifespan_initializes_and_releases_resources() -> None:
    """Verify that application resources are initialized and released."""
    database_url = "postgresql+asyncpg://test:password@localhost:5432/test_db"

    settings = SimpleNamespace(database_url=SecretStr(database_url))

    mock_engine = Mock()
    mock_engine.dispose = AsyncMock()

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
    ):
        with TestClient(app) as client:
            assert client.get("/health/live").status_code == 200
            assert app.state.session_factory is mock_session_factory

            mock_engine.dispose.assert_not_awaited()

        mock_get_settings.assert_called_once_with()
        mock_create_engine.assert_called_once_with(database_url)
        mock_create_session_factory.assert_called_once_with(mock_engine)

        mock_engine.dispose.assert_awaited_once_with()
