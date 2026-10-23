"""Live integration test for the configured embedding provider."""

import math
import os

import httpx
import pytest

from maintainer_lens.clients.embedding_factory import (
    create_embedding_client,
)
from maintainer_lens.core.config import get_settings


@pytest.mark.anyio
async def test_live_embedding_provider() -> None:
    """Verify the configured embedding provider against its real API."""

    if os.getenv("RUN_LIVE_EMBEDDING_TEST") != "1":
        pytest.skip("Live embedding test is disabled by default")

    settings = get_settings()

    api_key = settings.embedding_api_key

    if api_key is None or not api_key.get_secret_value().strip():
        pytest.fail("EMBEDDING_API_KEY must be configured for the live test")

    timeout = httpx.Timeout(30.0, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout) as http_client:
        provider = create_embedding_client(
            http_client=http_client,
            settings=settings,
        )

        vector = await provider.embed_query(
            "Where is the async worker retry logic implemented?"
        )

    assert len(vector) == settings.embedding_dimensions
    assert all(math.isfinite(value) for value in vector)

    print("\nEmbedding verification succeeded")
    print(f"Model: {settings.embedding_model}")
    print(f"Dimensions: {len(vector)}")
    print(f"Vector sample: {vector[:3]}")
