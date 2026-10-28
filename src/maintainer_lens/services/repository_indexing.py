"""Orchestrate repository ingestion, embedding, and index persistence."""

import asyncio
import logging
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.clients.github_repository import resolve_repository_revision
from maintainer_lens.clients.github_tree import fetch_repository_manifest
from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.investigation_budget import KnowledgePreparationBudget
from maintainer_lens.schemas.repository import RepositoryManifest, RepositoryRevision
from maintainer_lens.services.repository_index_lease import (
    RepositoryIndexLeaseLost,
    make_repository_index_identity_key,
    release_repository_index_lease,
    renew_repository_index_lease,
    try_acquire_repository_index_lease,
)
from maintainer_lens.services.repository_index_preparation import (
    prepare_source_index_contents,
)
from maintainer_lens.services.repository_index_writer import (
    EmbeddedChunk,
    RepositoryIndexConflict,
    write_repository_index,
)
from maintainer_lens.services.repository_sources import (
    download_repository_sources,
)
from maintainer_lens.services.source_chunker import chunk_repository_source

logger = logging.getLogger(__name__)


class RepositoryIndexBuildInProgress(RuntimeError):
    """Another worker currently owns the repository index build."""


async def _find_existing_index(
    session: AsyncSession,
    revision: RepositoryRevision,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    chunker_version: str,
) -> UUID | None:
    """Find a reusable index for an exact repository configuration."""

    async with session.begin():
        statement = select(RepositoryIndex).where(
            RepositoryIndex.repository == revision.repository,
            RepositoryIndex.commit_sha == revision.commit_sha,
            RepositoryIndex.embedding_provider == embedding_provider_name,
            RepositoryIndex.embedding_model == embedding_model,
            RepositoryIndex.embedding_dimensions == embedding_dimensions,
            RepositoryIndex.chunker_version == chunker_version,
        )

        result = await session.scalars(statement)
        existing = result.one_or_none()

        if existing is None:
            return None

        if existing.status != "ready":
            raise RepositoryIndexConflict("Existing repository index is not ready")

        if existing.tree_sha != revision.tree_sha:
            raise RepositoryIndexConflict(
                "Existing repository index has a different tree SHA"
            )

        return existing.id


async def embed_repository_chunks(
    provider: EmbeddingProvider,
    chunks: list[CodeChunk],
    *,
    batch_size: int = 20,
) -> list[EmbeddedChunk]:
    """Embed source chunks in ordered, bounded batches."""

    if batch_size <= 0:
        raise ValueError("Embedding batch size must be positive")

    if not chunks:
        return []

    embedded: list[EmbeddedChunk] = []

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]

        texts = [chunk.content for chunk in batch]

        vectors = await provider.embed_documents(texts)

        if len(vectors) != len(batch):
            raise ValueError("Embedding batch result count mismatch")

        for chunk, vector in zip(batch, vectors, strict=True):
            embedded.append(
                EmbeddedChunk(
                    chunk=chunk,
                    vector=vector,
                )
            )

    return embedded


async def _renew_lease_until_stopped(
    lease_session_factory: async_sessionmaker[AsyncSession],
    *,
    identity_key: str,
    owner_token: UUID,
    lease_seconds: int,
    renewal_interval_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    """Periodically renew a lease until the build finishes."""

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=renewal_interval_seconds,
            )
        except TimeoutError:
            pass
        else:
            return

        async with lease_session_factory() as lease_session:
            renewed = await renew_repository_index_lease(
                lease_session,
                identity_key=identity_key,
                owner_token=owner_token,
                lease_seconds=lease_seconds,
            )

        if not renewed:
            raise RepositoryIndexLeaseLost(
                "Repository index build lease renewal failed"
            )


async def build_repository_index(
    github_http_client: httpx.AsyncClient,
    embedding_provider_client: EmbeddingProvider,
    session: AsyncSession,
    repository: str,
    *,
    lease_session_factory: async_sessionmaker[AsyncSession],
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    chunker_version: str,
    embedding_batch_size: int = 20,
    download_concurrency: int = 4,
    lease_seconds: int = 300,
    renewal_interval_seconds: float | None = None,
    preparation_budget: KnowledgePreparationBudget | None = None,
) -> UUID:
    """Build and persist a complete vector index for a GitHub revision."""

    if preparation_budget is not None:
        preparation_budget = KnowledgePreparationBudget.model_validate(
            preparation_budget.model_dump()
        )

    if not repository.strip():
        raise ValueError("Repository cannot be empty")

    if embedding_dimensions <= 0:
        raise ValueError("Embedding dimensions must be positive")

    if embedding_batch_size <= 0:
        raise ValueError("Embedding batch size must be positive")

    if download_concurrency <= 0:
        raise ValueError("Download concurrency must be positive")

    if lease_seconds <= 0:
        raise ValueError("Lease duration must be positive")

    if any(
        not value.strip()
        for value in (
            embedding_provider_name,
            embedding_model,
            chunker_version,
        )
    ):
        raise ValueError("Index configuration fields cannot be empty")

    renewal_interval = (
        lease_seconds / 3
        if renewal_interval_seconds is None
        else renewal_interval_seconds
    )

    if not 0 < renewal_interval < lease_seconds:
        raise ValueError("Renewal interval must be between zero and lease duration")

    revision = await resolve_repository_revision(
        github_http_client,
        repository,
    )

    index_options = {
        "embedding_provider_name": embedding_provider_name,
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "chunker_version": chunker_version,
    }

    existing_index_id = await _find_existing_index(
        session,
        revision,
        **index_options,
    )

    if existing_index_id is not None:
        return existing_index_id

    identity_key = make_repository_index_identity_key(
        revision,
        **index_options,
    )

    async with lease_session_factory() as lease_session:
        owner_token = await try_acquire_repository_index_lease(
            lease_session,
            revision,
            **index_options,
            lease_seconds=lease_seconds,
        )

    if owner_token is None:
        existing_index_id = await _find_existing_index(
            session,
            revision,
            **index_options,
        )
        if existing_index_id is not None:
            return existing_index_id

        raise RepositoryIndexBuildInProgress(
            "Repository index is being built by another worker"
        )

    try:
        existing_index_id = await _find_existing_index(
            session,
            revision,
            **index_options,
        )

        if existing_index_id is not None:
            return existing_index_id

        stop_event = asyncio.Event()

        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(
                    _renew_lease_until_stopped(
                        lease_session_factory,
                        identity_key=identity_key,
                        owner_token=owner_token,
                        lease_seconds=lease_seconds,
                        renewal_interval_seconds=renewal_interval,
                        stop_event=stop_event,
                    )
                )

                try:
                    manifest = await fetch_repository_manifest(
                        github_http_client,
                        revision,
                    )

                    if preparation_budget is not None:
                        index_id = await prepare_source_index_contents(
                            github_http_client,
                            embedding_provider_client,
                            session,
                            revision,
                            manifest,
                            budget=preparation_budget,
                            embedding_provider_name=embedding_provider_name,
                            embedding_model=embedding_model,
                            embedding_dimensions=embedding_dimensions,
                            chunker_version=chunker_version,
                            embedding_batch_size=embedding_batch_size,
                            owner_token=owner_token,
                        )
                    else:
                        index_id = await _build_in_memory_index(
                            github_http_client,
                            embedding_provider_client,
                            session,
                            revision,
                            manifest,
                            embedding_provider_name=embedding_provider_name,
                            embedding_model=embedding_model,
                            embedding_dimensions=embedding_dimensions,
                            chunker_version=chunker_version,
                            embedding_batch_size=embedding_batch_size,
                            download_concurrency=download_concurrency,
                            owner_token=owner_token,
                        )

                finally:
                    stop_event.set()

        except* Exception as error_group:
            if len(error_group.exceptions) == 1:
                raise error_group.exceptions[0] from None

            raise

        return index_id

    finally:
        try:
            async with lease_session_factory() as lease_session:
                await release_repository_index_lease(
                    lease_session,
                    identity_key=identity_key,
                    owner_token=owner_token,
                )

        except Exception:
            logger.exception("Failed to release repository index build lease")


async def _build_in_memory_index(
    github_http_client: httpx.AsyncClient,
    embedding_provider_client: EmbeddingProvider,
    session: AsyncSession,
    revision: RepositoryRevision,
    manifest: RepositoryManifest,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    chunker_version: str,
    embedding_batch_size: int,
    download_concurrency: int,
    owner_token: UUID,
) -> UUID:
    """Preserve the original bounded builder for existing direct callers."""
    sources = await download_repository_sources(
        github_http_client, manifest, concurrency=download_concurrency
    )
    chunks: list[CodeChunk] = []
    for source in sources:
        chunks.extend(chunk_repository_source(source))
    if not chunks:
        raise ValueError(f"No indexable source chunks found for {revision.repository}")
    embedded_items = await embed_repository_chunks(
        embedding_provider_client, chunks, batch_size=embedding_batch_size
    )
    return await write_repository_index(
        session,
        revision,
        embedded_items,
        embedding_provider=embedding_provider_name,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        chunker_version=chunker_version,
        owner_token=owner_token,
    )
