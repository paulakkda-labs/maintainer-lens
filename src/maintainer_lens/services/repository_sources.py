"""Download repository source files with bounded concurrency."""

import asyncio
from collections import deque

import httpx

from maintainer_lens.clients.github_blob import fetch_repository_blob
from maintainer_lens.schemas.repository import (
    RepositoryManifest,
    RepositorySource,
)

MAX_IN_MEMORY_SOURCE_BYTES = 8 * 1024 * 1024
MAX_CONCURRENCY = 8


async def download_repository_sources(
    http_client: httpx.AsyncClient,
    manifest: RepositoryManifest,
    *,
    concurrency: int = 4,
) -> list[RepositorySource]:
    """Download and verify all files in a small repository manifest."""

    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError("Invalid repository download concurrency")

    total_bytes = sum(file.size for file in manifest.files)

    if total_bytes > MAX_IN_MEMORY_SOURCE_BYTES:
        raise ValueError("Repository source batch exceeds memory budget")

    pending = deque(enumerate(manifest.files))
    results: list[RepositorySource | None] = [None] * len(manifest.files)

    async def worker() -> None:
        while pending:
            index, file = pending.popleft()

            content = await fetch_repository_blob(
                http_client,
                manifest.repository,
                file,
            )

            results[index] = RepositorySource(
                file=file,
                content=content,
            )

    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(manifest.files))):
            group.create_task(worker())

    if any(source is None for source in results):
        raise RuntimeError("Repository source download incomplete")

    return [source for source in results if source is not None]
