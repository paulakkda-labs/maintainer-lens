"""Unit tests for asynchronous database engine configuration."""

from unittest.mock import patch

from maintainer_lens.db.engine import create_db_engine


def test_create_db_engine_configuration() -> None:
    """Verify that the engine is created with the expected pool settings."""
    database_url = "postgresql+asyncpg://test:password@localhost:5432/test_db"

    with patch("maintainer_lens.db.engine.create_async_engine") as mock_create_engine:
        engine = create_db_engine(database_url)

    mock_create_engine.assert_called_once_with(
        database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )

    assert engine is mock_create_engine.return_value
