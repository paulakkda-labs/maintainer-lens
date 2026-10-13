"""Application configuration and settings management."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load and validate application settings from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: SecretStr
    redis_url: SecretStr
    rabbitmq_url: SecretStr

    llm_base_url: str | None = None
    llm_model_name: str | None = None
    llm_api_key: SecretStr | None = None

    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    """Return a cached application settings instance."""
    return Settings()
