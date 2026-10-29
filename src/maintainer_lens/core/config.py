"""Application configuration and settings management."""

from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, model_validator
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
    github_token: SecretStr | None = None

    llm_base_url: str | None = None
    llm_model_name: str | None = None
    llm_api_key: SecretStr | None = None
    llm_max_output_tokens: int = Field(default=4096, gt=0)
    # Optional deployment approval ceiling, not a provider capability guarantee.
    llm_review_max_output_tokens: int | None = Field(default=None, gt=0)

    # Embedding provider configuration.
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_api_key: SecretStr | None = None

    embedding_dimensions: int = Field(default=1024, gt=0)
    embedding_max_batch_size: int = Field(default=20, gt=0)
    embedding_request_dimensions: int | None = Field(
        default=None,
        gt=0,
    )

    @model_validator(mode="after")
    def validate_review_output_ceiling(self) -> Self:
        if (
            self.llm_review_max_output_tokens is not None
            and self.llm_review_max_output_tokens < self.llm_max_output_tokens
        ):
            raise ValueError(
                "Review output ceiling cannot be below the initial output budget"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached application settings instance."""
    return Settings()
