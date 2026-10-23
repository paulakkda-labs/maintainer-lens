"""Tests for embedding client construction from application settings."""

import json

import httpx
import pytest
from pydantic import SecretStr

from maintainer_lens.clients.embedding_factory import (
    create_embedding_client,
)
from maintainer_lens.core.config import Settings


def make_settings(**overrides: object) -> Settings:
    """Create isolated settings for factory tests."""

    values = {
        "embedding_base_url": "https://embedding.example/v1",
        "embedding_model": "test-embedding-model",
        "embedding_api_key": SecretStr("fake-key"),
        "embedding_dimensions": 3,
        "embedding_max_batch_size": 2,
        "embedding_request_dimensions": None,
    }
    values.update(overrides)

    return Settings.model_construct(**values)


@pytest.mark.anyio
async def test_factory_uses_application_settings() -> None:
    """Pass model settings and authentication to the embedding client."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer fake-key"

        payload = json.loads(request.content)

        assert payload == {
            "model": "test-embedding-model",
            "input": ["find retry logic"],
            "encoding_format": "float",
        }

        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": 0,
                        "embedding": [0.1, 0.2, 0.3],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = create_embedding_client(
            http_client,
            make_settings(),
        )

        vector = await provider.embed_query("find retry logic")

    assert vector == [0.1, 0.2, 0.3]


@pytest.mark.anyio
async def test_factory_supports_provider_without_api_key() -> None:
    """Allow local embedding providers without bearer authentication."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers

        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": 0,
                        "embedding": [0.4, 0.5, 0.6],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = create_embedding_client(
            http_client,
            make_settings(embedding_api_key=None),
        )

        vector = await provider.embed_query("hello")

    assert vector == [0.4, 0.5, 0.6]


@pytest.mark.anyio
async def test_factory_rejects_dimension_mismatch() -> None:
    """Reject inconsistent dimension settings during construction."""

    async with httpx.AsyncClient() as http_client:
        with pytest.raises(ValueError, match="dimensions must match"):
            create_embedding_client(
                http_client,
                make_settings(
                    embedding_dimensions=1024,
                    embedding_request_dimensions=768,
                ),
            )


def test_embedding_settings_defaults() -> None:
    """Keep optional provider settings unset until configured."""

    fields = Settings.model_fields

    assert fields["embedding_base_url"].default is None
    assert fields["embedding_model"].default is None
    assert fields["embedding_api_key"].default is None
    assert fields["embedding_dimensions"].default == 1024
    assert fields["embedding_max_batch_size"].default == 20
    assert fields["embedding_request_dimensions"].default is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"embedding_base_url": None}, "EMBEDDING_BASE_URL"),
        ({"embedding_model": None}, "EMBEDDING_MODEL"),
    ],
)
async def test_factory_requires_provider_configuration(
    overrides: dict[str, object],
    message: str,
) -> None:
    """Reject missing provider settings before making HTTP requests."""

    async with httpx.AsyncClient() as http_client:
        with pytest.raises(ValueError, match=message):
            create_embedding_client(
                http_client,
                make_settings(**overrides),
            )
