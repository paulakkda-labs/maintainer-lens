"""Tests for the OpenAI-compatible embedding client."""

import json

import httpx
import pytest
from pydantic import SecretStr

from maintainer_lens.clients.embedding import (
    EmbeddingConfig,
    OpenAICompatibleEmbeddingClient,
)


def make_config(**overrides: object) -> EmbeddingConfig:
    """Create a small embedding configuration for isolated tests."""

    values = {
        "base_url": "https://embedding.example/compatible-mode/v1",
        "model": "qwen3.7-text-embedding-flash",
        "expected_dimensions": 2,
        "api_key": SecretStr("fake-embedding-key"),
        "max_batch_size": 20,
    }
    values.update(overrides)
    return EmbeddingConfig.model_validate(values)


def make_response(
    entries: list[dict[str, object]],
) -> httpx.Response:
    """Build an OpenAI-compatible embedding response."""

    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": "qwen3.7-text-embedding-flash",
            "data": entries,
        },
    )


@pytest.mark.anyio
async def test_embedding_batch_preserves_input_order() -> None:
    """Restore input order using provider response indexes."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/compatible-mode/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer fake-embedding-key"

        payload = json.loads(request.content)

        assert payload == {
            "model": "qwen3.7-text-embedding-flash",
            "input": ["first", "second"],
            "encoding_format": "float",
        }

        return make_response(
            [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = OpenAICompatibleEmbeddingClient(
            http_client,
            make_config(),
        )

        vectors = await provider.embed_documents(["first", "second"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


@pytest.mark.anyio
async def test_embedding_query_without_api_key() -> None:
    """Support providers that do not require bearer authentication."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        assert json.loads(request.content)["input"] == ["find login code"]

        return make_response([{"index": 0, "embedding": [0.5, 0.6]}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = OpenAICompatibleEmbeddingClient(
            http_client,
            make_config(api_key=None),
        )

        vector = await provider.embed_query("find login code")

    assert vector == [0.5, 0.6]


@pytest.mark.anyio
async def test_embedding_can_request_dimensions() -> None:
    """Only send dimensions when explicitly configured."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["dimensions"] == 2

        return make_response([{"index": 0, "embedding": [0.1, 0.2]}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = OpenAICompatibleEmbeddingClient(
            http_client,
            make_config(request_dimensions=2),
        )

        assert len(await provider.embed_query("hello")) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("texts", "entries", "message"),
    [
        (
            ["hello"],
            [{"index": 0, "embedding": [0.1]}],
            "dimension mismatch",
        ),
        (
            ["first", "second"],
            [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 0, "embedding": [0.3, 0.4]},
            ],
            "Duplicate embedding response index",
        ),
        (
            ["hello"],
            [{"index": 2, "embedding": [0.1, 0.2]}],
            "index out of range",
        ),
        (
            ["hello"],
            [],
            "response count mismatch",
        ),
    ],
)
async def test_embedding_rejects_invalid_responses(
    texts: list[str],
    entries: list[dict[str, object]],
    message: str,
) -> None:
    """Never accept incomplete or mismatched embedding results."""

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: make_response(entries))
    ) as http_client:
        provider = OpenAICompatibleEmbeddingClient(
            http_client,
            make_config(),
        )

        with pytest.raises(ValueError, match=message):
            await provider.embed_documents(texts)


@pytest.mark.anyio
async def test_embedding_rejects_non_finite_vector() -> None:
    """Reject infinity rather than storing an invalid vector."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(b'{"data":[{"index":0,"embedding":[1e400,0.1]}]}'),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = OpenAICompatibleEmbeddingClient(
            http_client,
            make_config(),
        )

        with pytest.raises(ValueError, match="non-finite"):
            await provider.embed_query("hello")


@pytest.mark.anyio
async def test_embedding_propagates_rate_limit_error() -> None:
    """Leave HTTP rate-limit handling to the calling service."""

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(429))
    ) as http_client:
        provider = OpenAICompatibleEmbeddingClient(
            http_client,
            make_config(),
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await provider.embed_query("hello")

    assert exc_info.value.response.status_code == 429


@pytest.mark.anyio
async def test_embedding_rejects_invalid_inputs() -> None:
    """Validate input batches before sending HTTP requests."""

    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Unexpected HTTP request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = OpenAICompatibleEmbeddingClient(
            http_client,
            make_config(max_batch_size=2),
        )

        for texts in [[], ["  "], ["a", "b", "c"]]:
            with pytest.raises(ValueError):
                await provider.embed_documents(texts)


def test_embedding_rejects_conflicting_dimensions() -> None:
    """Prevent mismatched requested and expected dimensions."""

    async_client = httpx.AsyncClient()

    try:
        with pytest.raises(ValueError, match="dimensions must match"):
            OpenAICompatibleEmbeddingClient(
                async_client,
                make_config(
                    expected_dimensions=1024,
                    request_dimensions=768,
                ),
            )
    finally:
        # A client not entered as a context manager is still unstarted.
        # It owns no live connections in this test.
        pass
