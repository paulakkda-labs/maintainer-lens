"""Live end-to-end verification of multilingual hybrid repository search."""

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
from maintainer_lens.services.repository_hybrid_search import (
    search_repository_hybrid,
)
from maintainer_lens.services.repository_index_lease import (
    make_repository_index_identity_key,
)
from maintainer_lens.services.repository_indexing import (
    build_repository_index,
)

REPOSITORY = "pypa/sampleproject"
PROVIDER_NAME = "dashscope"
KEYWORD = "add_one"
RANK_CONSTANT = 60

QUERIES = {
    "English": "Which function adds one to a given number?",
    "Chinese": "哪个函数会把传入的数字加一？",
}


@pytest.mark.anyio
async def test_live_multilingual_hybrid_search() -> None:
    """Build a real index and validate bilingual hybrid retrieval."""

    if os.getenv("RUN_LIVE_HYBRID_SEARCH_TEST") != "1":
        pytest.skip("Live hybrid search test is disabled")

    settings = get_settings()

    # Keep the version shorter than the VARCHAR(32) column limit.
    chunker_version = f"hybrid-e2e-{uuid4().hex[:16]}"

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
        "User-Agent": "MaintainerLens-hybrid-integration-test",
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
            embedding_provider = create_embedding_client(
                embedding_http_client,
                settings,
            )

            # Step 1: Build a real repository index.
            async with session_factory() as session:
                index_id = await build_repository_index(
                    github_http_client=github_client,
                    embedding_provider_client=embedding_provider,
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

            # Step 2: Read the persisted index and target chunks.
            async with session_factory() as session:
                index = await session.get(
                    RepositoryIndex,
                    index_id,
                )

                assert index is not None
                assert index.status == "ready"

                chunks = (
                    await session.scalars(
                        select(RepositoryChunk).where(
                            RepositoryChunk.index_id == index_id
                        )
                    )
                ).all()

                assert chunks

                target_keys = {
                    chunk.chunk_key
                    for chunk in chunks
                    if "def add_one" in chunk.content
                }

                assert target_keys, "The add_one function was not indexed"

                repository = index.repository
                commit_sha = index.commit_sha

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
            print("Live hybrid search verification")
            print(f"Repository: {repository}")
            print(f"Commit: {commit_sha}")
            print(f"Indexed chunks: {len(chunks)}")

            # Step 3: Search using both languages.
            for language, query in QUERIES.items():
                async with session_factory() as session:
                    results = await search_repository_hybrid(
                        session,
                        embedding_provider,
                        index_id,
                        query,
                        embedding_provider_name=PROVIDER_NAME,
                        embedding_model=settings.embedding_model,
                        keyword=KEYWORD,
                        candidate_limit=10,
                        top_k=5,
                        rank_constant=RANK_CONSTANT,
                    )

                assert results
                assert len(results) <= 5

                # Step 4: Verify ordering, identity, and evidence.
                assert len({item.chunk_key for item in results}) == len(results)

                scores = [item.rrf_score for item in results]

                assert scores == sorted(scores, reverse=True)
                assert all(math.isfinite(score) for score in scores)

                for item in results:
                    assert item.index_id == index_id
                    assert item.repository == repository
                    assert item.commit_sha == commit_sha
                    assert item.file_path
                    assert item.chunk_key
                    assert item.content
                    assert 1 <= item.start_line <= item.end_line

                    assert item.vector_rank is not None or item.keyword_rank is not None

                    expected_score = 0.0

                    if item.vector_rank is not None:
                        expected_score += 1 / (RANK_CONSTANT + item.vector_rank)

                    if item.keyword_rank is not None:
                        expected_score += 1 / (RANK_CONSTANT + item.keyword_rank)

                    assert item.rrf_score == pytest.approx(expected_score)

                # Step 5: Print the actual rankings.
                print()
                print(f"[{language}] {query}")

                for rank, item in enumerate(results, start=1):
                    print(
                        f"  {rank}. {item.file_path}"
                        f":{item.start_line}-{item.end_line}"
                        f" | RRF={item.rrf_score:.6f}"
                        f" | vector_rank={item.vector_rank}"
                        f" | keyword_rank={item.keyword_rank}"
                        f" | matched_in={item.matched_in}"
                    )

                # Step 6: Check the known target function.
                target = results[0]

                assert target.chunk_key in target_keys, (
                    f"{language} hybrid search did not rank add_one at Top-1"
                )

                assert target.vector_rank == 1
                assert target.keyword_rank == 1
                assert target.matched_in is not None
                assert target.cosine_distance is not None

                assert target.rrf_score == pytest.approx(2 / (RANK_CONSTANT + 1))

                evidence_url = (
                    f"https://github.com/{target.repository}"
                    f"/blob/{target.commit_sha}/{target.file_path}"
                    f"#L{target.start_line}-L{target.end_line}"
                )

                print(f"  Verified target: {target.file_path}")
                print(f"  Evidence: {evidence_url}")

            print()
            print("Both languages passed real hybrid search verification")

    finally:
        # Step 7: Clean up only the index created by this test.
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
