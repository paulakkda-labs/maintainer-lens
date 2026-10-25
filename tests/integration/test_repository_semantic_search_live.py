"""Live integration test for multilingual repository semantic search."""

import math
import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
from maintainer_lens.services.repository_indexing import (
    build_repository_index,
)
from maintainer_lens.services.repository_semantic_search import (
    search_repository_by_text,
)

REPOSITORY = "pypa/sampleproject"
PROVIDER_NAME = "dashscope"

QUERIES = {
    "English": "Which function adds one to a given number?",
    "Chinese": "哪个函数会把传入的数字加一？",
}


@pytest.mark.anyio
async def test_live_multilingual_repository_search() -> None:
    """Retrieve relevant English source code with both query languages."""

    if os.getenv("RUN_LIVE_MULTILINGUAL_SEARCH_TEST") != "1":
        pytest.skip("Live multilingual search test is disabled")

    settings = get_settings()

    assert settings.embedding_model is not None
    assert settings.embedding_dimensions > 0

    # Unique identity, within the VARCHAR(32) database limit.
    chunker_version = f"live-search-{uuid4().hex[:16]}"

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
            provider = create_embedding_client(
                embedding_http_client,
                settings,
            )

            # Step 1: Build a real source-code index.
            async with session_factory() as session:
                index_id = await build_repository_index(
                    github_http_client=github_client,
                    embedding_provider_client=provider,
                    session=session,
                    repository=REPOSITORY,
                    lease_session_factory=session_factory,
                    embedding_provider_name=PROVIDER_NAME,
                    embedding_model=settings.embedding_model,
                    embedding_dimensions=settings.embedding_dimensions,
                    chunker_version=chunker_version,
                    embedding_batch_size=settings.embedding_max_batch_size,
                    download_concurrency=4,
                )

            # Step 2: Verify the source and record its exact revision.
            async with session_factory() as session:
                index = await session.get(RepositoryIndex, index_id)

                assert index is not None
                assert index.status == "ready"

                commit_sha = index.commit_sha

                chunks = (
                    await session.scalars(
                        select(RepositoryChunk).where(
                            RepositoryChunk.index_id == index_id
                        )
                    )
                ).all()

                assert chunks

                # Make sure the target function was actually indexed.
                assert any("def add_one" in chunk.content for chunk in chunks), (
                    "The expected add_one source was not indexed"
                )

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

            print()
            print("Multilingual semantic search verification")
            print(f"Repository: {REPOSITORY}")
            print(f"Commit: {commit_sha}")
            print(f"Indexed chunks: {len(chunks)}")

            # Step 3: Search the same index in both languages.
            target_ranks: dict[str, int | None] = {}

            for language, query in QUERIES.items():
                async with session_factory() as session:
                    results = await search_repository_by_text(
                        session,
                        provider,
                        index_id,
                        query,
                        embedding_provider_name=PROVIDER_NAME,
                        embedding_model=settings.embedding_model,
                        top_k=5,
                    )

                assert results
                assert len(results) <= 5

                distances = [item.distance for item in results]

                assert all(math.isfinite(distance) for distance in distances)
                assert distances == sorted(distances)

                for item in results:
                    assert item.index_id == index_id
                    assert item.repository == REPOSITORY
                    assert item.commit_sha == commit_sha
                    assert item.file_path
                    assert item.content
                    assert len(item.chunk_key) == 64
                    assert 1 <= item.start_line <= item.end_line

                print()
                print(f"[{language}] {query}")

                target_rank = None

                for rank, item in enumerate(results, start=1):
                    print(
                        f"  {rank}. {item.file_path}"
                        f":{item.start_line}-{item.end_line}"
                        f" | distance={item.distance:.4f}"
                    )

                    if "def add_one" in item.content:
                        target_rank = rank

                target_ranks[language] = target_rank

                print(
                    f"  Target function rank: "
                    f"{target_rank if target_rank is not None else 'not in top 5'}"
                )

            # Step 4: Check basic retrieval relevance.
            assert target_ranks["English"] is not None, (
                "English query did not retrieve add_one in the top 5"
            )

            assert target_ranks["Chinese"] is not None, (
                "Chinese query did not retrieve add_one in the top 5"
            )

            print()
            print("Both languages retrieved the expected source function")

    finally:
        # Remove only the temporary index created by this test.
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
