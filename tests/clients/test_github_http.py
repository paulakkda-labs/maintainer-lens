"""Tests for optional GitHub HTTP client authentication."""

import pytest
from pydantic import SecretStr

from maintainer_lens.clients.github_http import create_github_http_client
from maintainer_lens.core.config import Settings


@pytest.mark.anyio
async def test_github_client_without_token() -> None:
    """Allow anonymous requests when no PAT is configured."""

    async with create_github_http_client() as client:
        request = client.build_request(
            "GET",
            "https://api.github.com/repos/pypa/sampleproject",
        )

        assert "Authorization" not in request.headers


@pytest.mark.anyio
async def test_github_client_with_token() -> None:
    """Include PAT authentication alongside request-specific headers."""

    async with create_github_http_client(SecretStr("fake-test-token")) as client:
        request = client.build_request(
            "GET",
            "https://api.github.com/repos/pypa/sampleproject",
            headers={
                "Accept": "application/vnd.github+json",
            },
        )

        assert request.headers["Authorization"] == ("Bearer fake-test-token")


def test_settings_loads_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read GitHub authentication from the environment."""

    monkeypatch.setenv("GITHUB_TOKEN", "fake-test-token")

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:pass@localhost/test",
        redis_url="redis://localhost/0",
        rabbitmq_url="amqp://guest:guest@localhost/vhost",
    )

    assert settings.github_token is not None
    assert settings.github_token.get_secret_value() == "fake-test-token"
    assert "fake-test-token" not in repr(settings.github_token)
