"""Live end-to-end verification of repository vector indexing."""

import math
import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.clients.embedding_factory import create_embedding_client
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.models.repository_index import (
    RepositoryChunk,
    RepositoryIndex,
)
from maintainer_lens.db.models.repository_index_lease import (
    RepositoryIndexBuildLease,
)
from maintainer_lens.schemas.repository import RepositoryRevision
from maintainer_lens.services.repository_index_lease import (
    make_repository_index_identity_key,
)
from maintainer_lens.services.repository_indexing import build_repository_index

REPOSITORY = "pypa/sampleproject"
PROVIDER_NAME = "dashscope"


class CountingEmbeddingProvider:
    """Count real document embedding requests."""

    def __init__(self, provider: EmbeddingProvider) -> None:
        self.provider = provider
        self.document_calls = 0

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self.document_calls += 1
        return await self.provider.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        return await self.provider.embed_query(text)


@pytest.mark.anyio
async def test_live_repository_index_build_and_reuse() -> None:
    """Build, verify, reuse, and clean up a real repository index."""

    if os.getenv("RUN_LIVE_REPOSITORY_INDEX_TEST") != "1":
        pytest.skip("Live repository index test is disabled")

    settings = get_settings()

    assert settings.embedding_model is not None
    assert settings.embedding_dimensions > 0

    # Give each test run a unique index identity.
    chunker_version = f"e2e-{uuid4().hex[:24]}"

    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )

    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )

    github_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "MaintainerLens-integration-test",
    }

    if settings.github_token is not None:
        github_headers["Authorization"] = (
            f"Bearer {settings.github_token.get_secret_value()}"
        )

    identity_key: str | None = None

    try:
        async with (
            httpx.AsyncClient(
                base_url="https://api.github.com",
                headers=github_headers,
                timeout=httpx.Timeout(60.0),
                follow_redirects=True,
            ) as github_client,
            httpx.AsyncClient(
                timeout=httpx.Timeout(60.0),
            ) as embedding_http_client,
        ):
            real_provider = create_embedding_client(
                embedding_http_client,
                settings,
            )

            provider = CountingEmbeddingProvider(real_provider)

            build_options = {
                "lease_session_factory": session_factory,
                "embedding_provider_name": PROVIDER_NAME,
                "embedding_model": settings.embedding_model,
                "embedding_dimensions": settings.embedding_dimensions,
                "chunker_version": chunker_version,
                "embedding_batch_size": settings.embedding_max_batch_size,
                "download_concurrency": 4,
            }

            # First execution: fetch, chunk, embed, and persist.
            async with session_factory() as session:
                first_index_id = await build_repository_index(
                    github_http_client=github_client,
                    embedding_provider_client=provider,
                    session=session,
                    repository=REPOSITORY,
                    **build_options,
                )

            first_embedding_calls = provider.document_calls

            assert first_embedding_calls > 0

            # Verify the committed index and stored vectors.
            async with session_factory() as session:
                index = await session.get(
                    RepositoryIndex,
                    first_index_id,
                )

                assert index is not None
                assert index.status == "ready"
                assert index.repository == REPOSITORY
                assert index.embedding_model == settings.embedding_model
                assert index.embedding_dimensions == settings.embedding_dimensions

                chunks = (
                    await session.scalars(
                        select(RepositoryChunk).where(
                            RepositoryChunk.index_id == first_index_id
                        )
                    )
                ).all()

                assert chunks

                for chunk in chunks:
                    assert chunk.content
                    assert chunk.file_path
                    assert len(chunk.embedding) == settings.embedding_dimensions
                    assert all(math.isfinite(float(value)) for value in chunk.embedding)

                revision = RepositoryRevision(
                    repository=index.repository,
                    default_branch="main",
                    commit_sha=index.commit_sha,
                    tree_sha=index.tree_sha,
                )

                identity_key = make_repository_index_identity_key(
                    revision,
                    embedding_provider_name=PROVIDER_NAME,
                    embedding_model=settings.embedding_model,
                    embedding_dimensions=settings.embedding_dimensions,
                    chunker_version=chunker_version,
                )

            # Second execution: reuse without another paid request.
            async with session_factory() as session:
                second_index_id = await build_repository_index(
                    github_http_client=github_client,
                    embedding_provider_client=provider,
                    session=session,
                    repository=REPOSITORY,
                    **build_options,
                )

            assert second_index_id == first_index_id
            assert provider.document_calls == first_embedding_calls

            # A completed build must not retain its lease.
            async with session_factory() as session:
                remaining_lease = await session.scalar(
                    select(RepositoryIndexBuildLease.identity_key).where(
                        RepositoryIndexBuildLease.identity_key == identity_key
                    )
                )

                assert remaining_lease is None

            print()
            print("Repository indexing verification succeeded")
            print(f"Repository: {REPOSITORY}")
            print(f"Index ID: {first_index_id}")
            print(f"Stored chunks: {len(chunks)}")
            print(f"Embedding dimensions: {settings.embedding_dimensions}")
            print(f"Initial embedding requests: {first_embedding_calls}")
            print("Second run: reused existing index")
            print("Additional embedding requests: 0")
            print("Remaining build lease: none")

    finally:
        # Remove only records owned by this unique test run.
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    delete(RepositoryIndex).where(
                        RepositoryIndex.repository == REPOSITORY,
                        RepositoryIndex.chunker_version == chunker_version,
                        RepositoryIndex.embedding_provider == PROVIDER_NAME,
                    )
                )

                if identity_key is not None:
                    await connection.execute(
                        delete(RepositoryIndexBuildLease).where(
                            RepositoryIndexBuildLease.identity_key == identity_key
                        )
                    )

        finally:
            await engine.dispose()
