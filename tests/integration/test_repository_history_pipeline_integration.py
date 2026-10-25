"""Integration tests for the complete repository history RAG pipeline."""

import os
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from maintainer_lens.clients.embedding_factory import create_embedding_client
from maintainer_lens.clients.github_http import create_github_http_client
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.services.repository_history_pipeline import (
    prepare_repository_history,
)
from maintainer_lens.services.repository_history_search import (
    search_repository_history,
)
from maintainer_lens.services.repository_history_semantic_search import (
    search_repository_history_semantic,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def _rollback_session_factory():
    """Give production services sessions backed by a rollback-only test transaction."""

    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())

    try:
        async with engine.connect() as connection:
            outer_transaction = await connection.begin()

            session_factory = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )

            try:
                yield session_factory
            finally:
                if outer_transaction.is_active:
                    await outer_transaction.rollback()
    finally:
        await engine.dispose()


class FakeEmbeddingProvider:
    """Return deterministic vectors without external model requests."""

    def __init__(self) -> None:
        self.documents_embedded = 0

    @staticmethod
    def _vector(text: str) -> list[float]:
        if "cancellation" in text.casefold():
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self.documents_embedded += len(texts)
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _issue(number: int, title: str, *, is_pr: bool = False) -> dict:
    path = "pull" if is_pr else "issues"

    result = {
        "number": number,
        "title": title,
        "body": f"Description of {title}",
        "state": "closed" if is_pr else "open",
        "html_url": f"https://github.com/org/repo/{path}/{number}",
        "created_at": "2026-10-20T00:00:00Z",
        "updated_at": "2026-10-25T00:00:00Z",
    }

    if is_pr:
        result["pull_request"] = {}

    return result


def _comment() -> dict:
    return {
        "id": 9001,
        "body": "Cancellation recovery was discussed here.",
        "html_url": ("https://github.com/org/repo/issues/100#issuecomment-9001"),
        "created_at": "2026-10-21T00:00:00Z",
        "updated_at": "2026-10-22T00:00:00Z",
        "user": {"login": "maintainer"},
    }


def _pr_details() -> dict:
    return {
        "number": 101,
        "html_url": "https://github.com/org/repo/pull/101",
        "merged": True,
        "merged_at": "2026-10-24T00:00:00Z",
        "merge_commit_sha": "c" * 40,
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40},
        "changed_files": 2,
    }


async def test_history_pipeline_integrates_existing_services() -> None:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration is disabled")

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests.append(path)

        if path == "/repos/org/repo/issues":
            return httpx.Response(
                200,
                json=[
                    _issue(100, "AsyncClient cancellation"),
                    _issue(101, "Fix connection pooling", is_pr=True),
                ],
            )

        if path == "/repos/org/repo/issues/100/comments":
            return httpx.Response(200, json=[_comment()])

        if path == "/repos/org/repo/issues/101/comments":
            return httpx.Response(200, json=[])

        if path == "/repos/org/repo/pulls/101":
            return httpx.Response(200, json=_pr_details())

        raise AssertionError(f"Unexpected GitHub request: {request.url}")

    embedding = FakeEmbeddingProvider()

    async with _rollback_session_factory() as session_factory:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as github_client:

            async def run_pipeline():
                return await prepare_repository_history(
                    session_factory,
                    github_client,
                    embedding,
                    "org/repo",
                    embedding_provider_name="test-provider",
                    embedding_model="test-model",
                    embedding_dimensions=3,
                    max_items=2,
                    detail_items=2,
                    comments_per_page=5,
                )

            first = await run_pipeline()

            assert first.items_collected == 2
            assert first.comments_collected == 1
            assert first.pull_requests_collected == 1
            assert first.documents_found == 3
            assert first.documents_embedded == 3
            assert first.documents_reused == 0

            second = await run_pipeline()

            assert second.documents_found == 3
            assert second.documents_embedded == 0
            assert second.documents_reused == 3
            assert embedding.documents_embedded == 3

        async with session_factory() as session:
            keyword_hits = await search_repository_history(
                session,
                "org/repo",
                "cancellation",
                top_k=5,
            )

            semantic_hits = await search_repository_history_semantic(
                session,
                embedding,
                "org/repo",
                "cancellation",
                embedding_provider_name="test-provider",
                embedding_model="test-model",
                embedding_dimensions=3,
                top_k=3,
            )

            assert keyword_hits
            assert semantic_hits
            assert all(hit.repository == "org/repo" for hit in semantic_hits)
            assert any(
                hit.source_url.endswith("#issuecomment-9001") for hit in semantic_hits
            )

            pr = await session.get(
                RepositoryHistoryRecord,
                ("org/repo", 101),
            )
            assert pr is not None
            assert pr.kind == "pull_request"

    assert "/repos/org/repo/issues" in requests
    assert "/repos/org/repo/pulls/101" in requests


async def test_live_repository_history_rag_end_to_end() -> None:
    if os.getenv("RUN_LIVE_HISTORY_E2E") != "1":
        pytest.skip("Live history E2E is disabled")

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration is disabled")

    settings = get_settings()

    if not settings.embedding_base_url or not settings.embedding_model:
        pytest.fail("Configure EMBEDDING_BASE_URL and EMBEDDING_MODEL")

    repository = os.getenv("HISTORY_E2E_REPOSITORY", "encode/httpx")
    provider_name = "openai-compatible"
    model_name = settings.embedding_model
    dimensions = settings.embedding_dimensions

    async with _rollback_session_factory() as session_factory:
        async with (
            create_github_http_client(settings.github_token) as github_client,
            httpx.AsyncClient(timeout=60.0) as embedding_http_client,
        ):
            embedding_client = create_embedding_client(
                embedding_http_client,
                settings,
            )

            async def run_pipeline():
                return await prepare_repository_history(
                    session_factory,
                    github_client,
                    embedding_client,
                    repository,
                    embedding_provider_name=provider_name,
                    embedding_model=model_name,
                    embedding_dimensions=dimensions,
                    max_items=3,
                    detail_items=1,
                    comments_per_page=5,
                    embedding_batch_size=min(
                        5,
                        settings.embedding_max_batch_size,
                    ),
                )

            first = await run_pipeline()

            assert first.items_collected > 0
            assert first.documents_found >= first.items_collected

            async with session_factory() as session:
                newest = await session.scalar(
                    select(RepositoryHistoryRecord)
                    .where(RepositoryHistoryRecord.repository == repository)
                    .order_by(RepositoryHistoryRecord.source_updated_at.desc())
                    .limit(1)
                )

                assert newest is not None
                keyword = newest.title.split()[0]

                keyword_hits = await search_repository_history(
                    session,
                    repository,
                    keyword,
                    top_k=5,
                )

                semantic_hits = await search_repository_history_semantic(
                    session,
                    embedding_client,
                    repository,
                    newest.title,
                    embedding_provider_name=provider_name,
                    embedding_model=model_name,
                    embedding_dimensions=dimensions,
                    top_k=3,
                )

                assert keyword_hits
                assert semantic_hits

                for hit in semantic_hits:
                    assert hit.repository == repository
                    assert hit.source_url.startswith(
                        f"https://github.com/{repository}/"
                    )

            second = await run_pipeline()

            print(
                "\nHISTORY RAG LIVE E2E:",
                repository,
                "collected=",
                first.items_collected,
                "comments=",
                first.comments_collected,
                "PR details=",
                first.pull_requests_collected,
                "embedded=",
                first.documents_embedded,
                "reused_on_second_run=",
                second.documents_reused,
            )
