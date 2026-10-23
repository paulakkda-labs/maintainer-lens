"""OpenAI-compatible text embedding client."""

import math
from typing import Protocol

import httpx
from pydantic import BaseModel, Field, SecretStr


class EmbeddingConfig(BaseModel):
    """Configuration for an OpenAI-compatible embedding provider."""

    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    expected_dimensions: int = Field(gt=0)
    api_key: SecretStr | None = None
    max_batch_size: int = Field(default=20, gt=0)
    request_dimensions: int | None = Field(default=None, gt=0)


class EmbeddingProvider(Protocol):
    """The embedding operations required by repository retrieval."""

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class _EmbeddingItem(BaseModel):
    """A vector and its corresponding input position."""

    index: int = Field(ge=0)
    embedding: list[float]


class _EmbeddingResponse(BaseModel):
    """The relevant fields of an OpenAI-compatible response."""

    data: list[_EmbeddingItem]


class OpenAICompatibleEmbeddingClient:
    """Generate and validate dense vectors using an HTTPX client."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        config: EmbeddingConfig,
    ) -> None:
        url = httpx.URL(config.base_url)

        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("Invalid embedding base URL")

        if url.query or url.fragment:
            raise ValueError("Embedding base URL cannot contain query or fragment")

        if (
            config.request_dimensions is not None
            and config.request_dimensions != config.expected_dimensions
        ):
            raise ValueError("Requested and expected dimensions must match")

        self._http_client = http_client
        self._config = config
        self._endpoint = f"{str(url).rstrip('/')}/embeddings"

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        """Embed a bounded batch and preserve the original input order."""

        if not 1 <= len(texts) <= self._config.max_batch_size:
            raise ValueError("Invalid embedding batch size")

        if any(not text.strip() for text in texts):
            raise ValueError("Embedding input cannot be empty")

        payload: dict[str, object] = {
            "model": self._config.model,
            "input": texts,
            "encoding_format": "float",
        }

        if self._config.request_dimensions is not None:
            payload["dimensions"] = self._config.request_dimensions

        headers = {"Accept": "application/json"}

        if self._config.api_key is not None:
            token = self._config.api_key.get_secret_value()

            if token:
                headers["Authorization"] = f"Bearer {token}"

        response = await self._http_client.post(
            self._endpoint,
            json=payload,
            headers=headers,
        )
        response.raise_for_status()

        result = _EmbeddingResponse.model_validate(response.json())

        if len(result.data) != len(texts):
            raise ValueError("Embedding response count mismatch")

        ordered: list[list[float] | None] = [None] * len(texts)

        for item in result.data:
            if item.index >= len(texts):
                raise ValueError("Embedding response index out of range")

            if ordered[item.index] is not None:
                raise ValueError("Duplicate embedding response index")

            if len(item.embedding) != self._config.expected_dimensions:
                raise ValueError("Embedding dimension mismatch")

            if not all(math.isfinite(value) for value in item.embedding):
                raise ValueError("Embedding contains non-finite values")

            ordered[item.index] = item.embedding

        if any(vector is None for vector in ordered):
            raise ValueError("Embedding response is incomplete")

        return [vector for vector in ordered if vector is not None]

    async def embed_query(self, text: str) -> list[float]:
        """Embed one search query using the same configured model."""

        vectors = await self.embed_documents([text])
        return vectors[0]
