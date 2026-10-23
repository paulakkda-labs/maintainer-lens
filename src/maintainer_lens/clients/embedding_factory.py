"""Construct embedding clients from application settings."""

import httpx

from maintainer_lens.clients.embedding import (
    EmbeddingConfig,
    OpenAICompatibleEmbeddingClient,
)
from maintainer_lens.core.config import Settings


def create_embedding_client(
    http_client: httpx.AsyncClient,
    settings: Settings,
) -> OpenAICompatibleEmbeddingClient:
    """Build an embedding client using the configured provider."""

    base_url = settings.embedding_base_url
    model = settings.embedding_model

    if not base_url or not base_url.strip():
        raise ValueError("EMBEDDING_BASE_URL must be configured")

    if not model or not model.strip():
        raise ValueError("EMBEDDING_MODEL must be configured")

    config = EmbeddingConfig(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        expected_dimensions=settings.embedding_dimensions,
        max_batch_size=settings.embedding_max_batch_size,
        request_dimensions=settings.embedding_request_dimensions,
    )

    return OpenAICompatibleEmbeddingClient(
        http_client=http_client,
        config=config,
    )
