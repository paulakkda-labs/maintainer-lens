"""Orchestrate GitHub repository metadata ingestion."""

import httpx

from maintainer_lens.clients.github_repository import (
    resolve_repository_revision,
)
from maintainer_lens.clients.github_tree import fetch_repository_manifest
from maintainer_lens.schemas.repository import RepositoryManifest


async def build_repository_manifest(
    http_client: httpx.AsyncClient,
    repository: str,
) -> RepositoryManifest:
    """Resolve a repository revision and fetch its complete file manifest."""

    revision = await resolve_repository_revision(
        http_client,
        repository,
    )

    manifest = await fetch_repository_manifest(
        http_client,
        revision,
    )

    return manifest
