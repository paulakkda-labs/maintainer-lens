"""Evaluate real repository retrieval against a pinned GitHub commit.

This test performs real GitHub calls, paid embedding requests, and PostgreSQL
indexing. It uses a unique index identity and deletes all its data afterward.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select

from maintainer_lens.clients.embedding_factory import create_embedding_client
from maintainer_lens.clients.github import GITHUB_API_BASE_URL, GITHUB_API_VERSION
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.models.repository_index import RepositoryChunk, RepositoryIndex
from maintainer_lens.db.models.repository_index_lease import RepositoryIndexBuildLease
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.evaluation.retrieval_comparison import (
    compare_repository_retrieval,
)
from maintainer_lens.evaluation.retrieval_dataset import load_retrieval_dataset
from maintainer_lens.schemas.repository import RepositoryRevision
from maintainer_lens.services import repository_indexing
from maintainer_lens.services.repository_index_lease import (
    make_repository_index_identity_key,
)
from maintainer_lens.services.repository_query_analysis import analyze_repository_query

DATASET_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "retrieval"
    / "sampleproject.json"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_real_repository_retrieval_quality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.getenv("RUN_LIVE_RETRIEVAL_EVALUATION") != "1":
        pytest.skip("Live retrieval evaluation is disabled")

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration is disabled")

    dataset = load_retrieval_dataset(DATASET_PATH)
    settings = get_settings()

    if not settings.embedding_model or not settings.embedding_base_url:
        pytest.fail("Configure EMBEDDING_MODEL and EMBEDDING_BASE_URL")

    provider_name = "openai-compatible"
    model_name = settings.embedding_model
    dimensions = settings.embedding_dimensions
    chunker_version = f"eval-{uuid4().hex[:20]}"

    engine = create_db_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)
    revision: RepositoryRevision | None = None

    try:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if settings.github_token is not None:
            token = settings.github_token.get_secret_value()
            if token:
                headers["Authorization"] = f"Bearer {token}"

        async with (
            httpx.AsyncClient(timeout=60.0, headers=headers) as github_http_client,
            httpx.AsyncClient(timeout=60.0) as embedding_http_client,
        ):
            # Read the real default branch for accurate revision metadata.
            repository_response = await github_http_client.get(
                f"{GITHUB_API_BASE_URL}/repos/{dataset.repository}"
            )
            repository_response.raise_for_status()
            default_branch = repository_response.json()["default_branch"]

            # Resolve the immutable tree for the dataset's historical commit.
            response = await github_http_client.get(
                f"{GITHUB_API_BASE_URL}/repos/{dataset.repository}"
                f"/git/commits/{dataset.commit_sha}"
            )
            response.raise_for_status()
            commit_data = response.json()
            if commit_data["sha"] != dataset.commit_sha:
                raise ValueError("GitHub returned an unexpected commit SHA")

            revision = RepositoryRevision(
                repository=dataset.repository,
                default_branch=default_branch,
                commit_sha=dataset.commit_sha,
                tree_sha=commit_data["tree"]["sha"],
            )

            # The current build API resolves the latest default-branch revision.
            # Override only that boundary; ingestion, embedding and persistence
            # remain real and use the pinned revision above.
            monkeypatch.setattr(
                repository_indexing,
                "resolve_repository_revision",
                AsyncMock(return_value=revision),
            )

            embedding_client = create_embedding_client(embedding_http_client, settings)

            async with session_factory() as session:
                index_id = await repository_indexing.build_repository_index(
                    github_http_client,
                    embedding_client,
                    session,
                    dataset.repository,
                    lease_session_factory=session_factory,
                    embedding_provider_name=provider_name,
                    embedding_model=model_name,
                    embedding_dimensions=dimensions,
                    chunker_version=chunker_version,
                )

            async with session_factory() as session:
                result = await compare_repository_retrieval(
                    session,
                    embedding_client,
                    index_id,
                    dataset,
                    embedding_provider_name=provider_name,
                    embedding_model=model_name,
                    top_k=5,
                    candidate_limit=10,
                    rank_constant=60,
                )

            async with session_factory() as session:
                chunk_rows = (
                    await session.execute(
                        select(
                            RepositoryChunk.chunk_key,
                            RepositoryChunk.file_path,
                            RepositoryChunk.start_line,
                            RepositoryChunk.end_line,
                        ).where(RepositoryChunk.index_id == index_id)
                    )
                ).all()
            locations = {
                key: f"{path}:{start}-{end}" for key, path, start, end in chunk_rows
            }

            print(
                "\nREAL RETRIEVAL EVALUATION:",
                dataset.repository,
                dataset.commit_sha,
            )
            print(
                "Semantic  Recall@5:",
                f"{result.semantic.mean_recall_at_k:.3f}",
                "MRR@5:",
                f"{result.semantic.mrr_at_k:.3f}",
            )
            print(
                "Hybrid    Recall@5:",
                f"{result.automatic_hybrid.mean_recall_at_k:.3f}",
                "MRR@5:",
                f"{result.automatic_hybrid.mrr_at_k:.3f}",
            )

            for case, semantic, hybrid in zip(
                result.cases,
                result.semantic.query_metrics,
                result.automatic_hybrid.query_metrics,
                strict=True,
            ):
                keyword = analyze_repository_query(case.query).keyword
                print(
                    f"{case.case_id:30} keyword={str(keyword):12} "
                    f"semantic_rank={str(semantic.first_relevant_rank):>4} "
                    f"hybrid_rank={str(hybrid.first_relevant_rank):>4}"
                )
                print(
                    "  semantic:",
                    [locations.get(key, key) for key in case.semantic_chunk_keys],
                )
                print(
                    "  hybrid:  ",
                    [
                        locations.get(key, key)
                        for key in case.automatic_hybrid_chunk_keys
                    ],
                )

            # This tiny two-target dataset is a smoke-test quality gate, not
            # proof that the same ranking quality generalizes to real Issues.
            assert result.automatic_hybrid.mean_recall_at_k == 1.0, (
                "Expected every labeled evidence target within Hybrid Top-5"
            )
            assert (
                result.automatic_hybrid.mrr_at_k >= result.semantic.mrr_at_k - 1e-12
            ), "Hybrid MRR@5 regressed against semantic search on this dataset"

    finally:
        try:
            # Clean up even if build or evaluation fails. The unique chunker
            # version limits deletion to this test's repository index identity.
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(RepositoryIndex).where(
                            RepositoryIndex.repository == dataset.repository,
                            RepositoryIndex.commit_sha == dataset.commit_sha,
                            RepositoryIndex.embedding_provider == provider_name,
                            RepositoryIndex.embedding_model == model_name,
                            RepositoryIndex.embedding_dimensions == dimensions,
                            RepositoryIndex.chunker_version == chunker_version,
                        )
                    )
                    if revision is not None:
                        lease_key = make_repository_index_identity_key(
                            revision,
                            embedding_provider_name=provider_name,
                            embedding_model=model_name,
                            embedding_dimensions=dimensions,
                            chunker_version=chunker_version,
                        )
                        await session.execute(
                            delete(RepositoryIndexBuildLease).where(
                                RepositoryIndexBuildLease.identity_key == lease_key
                            )
                        )

            async with session_factory() as session:
                remaining = await session.scalar(
                    select(RepositoryIndex.id).where(
                        RepositoryIndex.repository == dataset.repository,
                        RepositoryIndex.commit_sha == dataset.commit_sha,
                        RepositoryIndex.embedding_provider == provider_name,
                        RepositoryIndex.embedding_model == model_name,
                        RepositoryIndex.embedding_dimensions == dimensions,
                        RepositoryIndex.chunker_version == chunker_version,
                    )
                )
                assert remaining is None, "Live evaluation left a repository index"
        finally:
            await engine.dispose()
