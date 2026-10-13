"""Unit tests for application settings and configuration caching."""

import pytest
from pydantic import SecretStr, ValidationError

from maintainer_lens.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def mock_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide isolated test credentials for required settings."""
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://test:db_password@localhost:5432/test_db",
    )
    monkeypatch.setenv(
        "REDIS_URL",
        "redis://:redis_password@localhost:6379/0",
    )
    monkeypatch.setenv(
        "RABBITMQ_URL",
        "amqp://test:rabbitmq_password@localhost:5672/",
    )


def test_required_settings() -> None:
    """Verify that required service URLs are loaded correctly."""
    settings = Settings(_env_file=None)

    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
    assert settings.redis_url.get_secret_value().startswith("redis://")
    assert settings.rabbitmq_url.get_secret_value().startswith("amqp://")


@pytest.mark.parametrize(
    "variable",
    ["DATABASE_URL", "REDIS_URL", "RABBITMQ_URL"],
)
def test_missing_required_settings(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    """Verify that missing required settings raise validation errors."""
    monkeypatch.delenv(variable)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_optional_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that empty optional settings fall back to None."""
    optional_variables = [
        "LLM_BASE_URL",
        "LLM_MODEL_NAME",
        "LLM_API_KEY",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_API_KEY",
    ]

    for variable in optional_variables:
        monkeypatch.setenv(variable, "")

    settings = Settings(_env_file=None)

    assert settings.llm_base_url is None
    assert settings.llm_model_name is None
    assert settings.llm_api_key is None

    assert settings.embedding_base_url is None
    assert settings.embedding_model is None
    assert settings.embedding_api_key is None


def test_secret_values_are_masked() -> None:
    """Verify that sensitive configuration values are masked."""
    settings = Settings(_env_file=None)

    assert isinstance(settings.database_url, SecretStr)
    assert isinstance(settings.redis_url, SecretStr)
    assert isinstance(settings.rabbitmq_url, SecretStr)

    assert "db_password" not in repr(settings)
    assert "redis_password" not in repr(settings)
    assert "rabbitmq_password" not in repr(settings)

    assert settings.database_url.get_secret_value().find("db_password") != -1


def test_settings_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that settings are cached until the cache is cleared."""
    get_settings.cache_clear()

    try:
        first = get_settings()

        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql+asyncpg://test:new_password@localhost:5432/test_db",
        )

        second = get_settings()

        assert first is second

        get_settings.cache_clear()

        third = get_settings()

        assert third is not first
        assert "new_password" in third.database_url.get_secret_value()

    finally:
        get_settings.cache_clear()
