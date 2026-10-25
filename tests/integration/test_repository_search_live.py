"""Live integration tests for automatic multilingual repository search."""

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
from maintainer_lens.services.repository_query_analysis import (
    analyze_repository_query,
)
from maintainer_lens.services.repository_search import (
    search_repository,
)

REPOSITORY = "pypa/sampleproject"
PROVIDER_NAME = "dashscope"
RANK_CONSTANT = 60

QUERY_CASES = (
    (
        "English semantic",
        "Which function adds one to a given number?",
        None,
    ),
    (
        "Chinese semantic",
        "哪个函数会把传入的数字加一？",
        None,
    ),
    (
        "English hybrid",
        "Which function implements `add_one()` to increment a number?",
        "add_one",
    ),
    (
        "Chinese hybrid",
        "哪个函数 `add_one()` 会把传入的数字加一？",
        "add_one",
    ),
)


@pytest.mark.anyio
async def test_live_automatic_multilingual_repository_search() -> None:
    """Verify automatic keyword selection and real repository retrieval."""

    if os.getenv("RUN_LIVE_AUTOMATIC_SEARCH_TEST") != "1":
        pytest.skip("Live automatic repository search test is disabled")

    settings = get_settings()

    # Use an isolated identity within the VARCHAR(32) column limit.
    chunker_version = f"auto-e2e-{uuid4().hex[:16]}"

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
        "User-Agent": "MaintainerLens-automatic-search-test",
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

            # Step 2: Check the persisted index and expected source.
            async with session_factory() as session:
                index = await session.get(RepositoryIndex, index_id)

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
                    if (
                        chunk.file_path == "src/sample/simple.py"
                        and "def add_one" in chunk.content
                    )
                }

                assert target_keys, "Expected add_one source was not indexed"

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
            print("Automatic multilingual repository search")
            print(f"Repository: {repository}")
            print(f"Commit: {commit_sha}")
            print(f"Indexed chunks: {len(chunks)}")

            # Step 3: Evaluate queries with and without identifiers.
            for language, query, expected_keyword in QUERY_CASES:
                analysis = analyze_repository_query(query)

                assert analysis.query == query
                assert analysis.keyword == expected_keyword

                async with session_factory() as session:
                    results = await search_repository(
                        session,
                        embedding_provider,
                        index_id,
                        query,
                        embedding_provider_name=PROVIDER_NAME,
                        embedding_model=settings.embedding_model,
                        candidate_limit=10,
                        top_k=5,
                        rank_constant=RANK_CONSTANT,
                    )

                # Step 4: Verify ordering and provenance.
                assert 1 <= len(results) <= 5

                assert len({item.chunk_key for item in results}) == len(results)

                assert results == sorted(
                    results,
                    key=lambda item: (
                        -item.rrf_score,
                        item.chunk_key,
                    ),
                )

                for item in results:
                    assert item.index_id == index_id
                    assert item.repository == repository
                    assert item.commit_sha == commit_sha
                    assert item.file_path
                    assert item.content
                    assert item.chunk_key
                    assert 1 <= item.start_line <= item.end_line
                    assert math.isfinite(item.rrf_score)

                    expected_score = 0.0

                    if item.vector_rank is not None:
                        expected_score += 1 / (RANK_CONSTANT + item.vector_rank)
                        assert item.cosine_distance is not None
                        assert math.isfinite(item.cosine_distance)
                    else:
                        assert item.cosine_distance is None

                    if item.keyword_rank is not None:
                        expected_score += 1 / (RANK_CONSTANT + item.keyword_rank)
                        assert item.matched_in is not None
                    else:
                        assert item.matched_in is None

                    assert item.rrf_score == pytest.approx(expected_score)

                # Step 5: Locate the expected source in the results.
                target_rank = next(
                    (
                        rank
                        for rank, item in enumerate(results, start=1)
                        if item.chunk_key in target_keys
                    ),
                    None,
                )

                assert target_rank is not None, (
                    f"{language} did not retrieve add_one in Top-5"
                )

                target = results[target_rank - 1]

                # Step 6: Validate automatic retrieval strategy.
                if expected_keyword is None:
                    assert target_rank == 1
                    assert all(
                        item.keyword_rank is None and item.matched_in is None
                        for item in results
                    )
                    assert all(item.vector_rank is not None for item in results)
                else:
                    assert target.keyword_rank == 1
                    assert target.matched_in is not None
                    assert target.vector_rank is not None

                print()
                print(f"[{language}] {query}")
                print(f"  Extracted keyword: {analysis.keyword}")

                for rank, item in enumerate(results, start=1):
                    print(
                        f"  {rank}. {item.file_path}"
                        f":{item.start_line}-{item.end_line}"
                        f" | RRF={item.rrf_score:.6f}"
                        f" | vector_rank={item.vector_rank}"
                        f" | keyword_rank={item.keyword_rank}"
                    )

                evidence_url = (
                    f"https://github.com/{target.repository}"
                    f"/blob/{target.commit_sha}/{target.file_path}"
                    f"#L{target.start_line}-L{target.end_line}"
                )

                print(f"  Target rank: {target_rank}")
                print(f"  Evidence: {evidence_url}")

            print()
            print("All automatic multilingual search cases passed")

    finally:
        # Step 7: Remove only this test's temporary index and lease.
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
