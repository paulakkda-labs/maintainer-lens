"""Integration tests for GitHub history embedding and semantic retrieval."""

import math
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from maintainer_lens.clients.github_history import GitHubHistoryItem
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.models.repository_history_details import (
    RepositoryHistoryCommentRecord,
)
from maintainer_lens.db.models.repository_history_embedding import (
    RepositoryHistoryEmbedding,
)
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.services.repository_history_collection import (
    RepositoryHistoryBatch,
)
from maintainer_lens.services.repository_history_indexing import (
    _validate_vector,
    index_repository_history,
)
from maintainer_lens.services.repository_history_persistence import (
    save_repository_history_batch,
)
from maintainer_lens.services.repository_history_semantic_search import (
    search_repository_history_semantic,
)

pytestmark = pytest.mark.anyio

BASE_TIME = datetime(2026, 10, 25, tzinfo=UTC)
PROVIDER = "test-provider"
MODEL = "test-history-embedding"
DIMENSIONS = 3


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeEmbeddingProvider:
    """Deterministic vectors without external API calls."""

    def __init__(self) -> None:
        self.document_calls = 0
        self.documents_embedded = 0
        self.query_calls = 0

    @staticmethod
    def _vector(text: str) -> list[float]:
        normalized = text.casefold()

        if "cancellation" in normalized:
            return [1.0, 0.0, 0.0]

        if "validation" in normalized:
            return [0.0, 1.0, 0.0]

        return [0.0, 0.0, 1.0]

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        self.document_calls += 1
        self.documents_embedded += len(texts)
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._vector(text)


@pytest.fixture
async def history_db():
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration test is disabled")

    repository = f"ml-test-{uuid4().hex}/history"
    other_repository = f"ml-test-{uuid4().hex}/history"

    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)

    repositories = (repository, other_repository)

    try:
        yield repository, other_repository, session_factory
    finally:
        try:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(RepositoryHistoryEmbedding).where(
                            RepositoryHistoryEmbedding.repository.in_(repositories)
                        )
                    )
                    await session.execute(
                        delete(RepositoryHistoryCommentRecord).where(
                            RepositoryHistoryCommentRecord.repository.in_(repositories)
                        )
                    )
                    await session.execute(
                        delete(RepositoryHistoryRecord).where(
                            RepositoryHistoryRecord.repository.in_(repositories)
                        )
                    )
        finally:
            await engine.dispose()


def _item(
    repository: str,
    number: int,
    title: str,
    *,
    updated_at: datetime = BASE_TIME,
) -> GitHubHistoryItem:
    return GitHubHistoryItem.model_validate(
        {
            "repository": repository,
            "number": number,
            "kind": "issue",
            "title": title,
            "body": f"Description for {title}",
            "state": "open",
            "html_url": f"https://github.com/{repository}/issues/{number}",
            "created_at": BASE_TIME - timedelta(days=10),
            "updated_at": updated_at,
        }
    )


async def _save_items(
    session_factory,
    repository: str,
    *items: GitHubHistoryItem,
) -> None:
    async with session_factory() as session:
        await save_repository_history_batch(
            session,
            repository,
            RepositoryHistoryBatch(
                items=items,
                pages_fetched=1,
                stop_reason="exhausted",
            ),
        )


async def test_history_semantic_index_reuse_refresh_and_isolation(
    history_db,
) -> None:
    repository, other_repository, session_factory = history_db

    issue = _item(
        repository,
        1,
        "AsyncClient cancellation",
    )
    other_issue = _item(
        repository,
        2,
        "Schema validation",
    )
    unrelated = _item(
        other_repository,
        1,
        "AsyncClient cancellation",
    )

    await _save_items(session_factory, repository, issue, other_issue)
    await _save_items(session_factory, other_repository, unrelated)

    async with session_factory() as session:
        async with session.begin():
            session.add(
                RepositoryHistoryCommentRecord(
                    repository=repository,
                    issue_number=1,
                    comment_id=9001,
                    author_login="maintainer",
                    body="Cancellation recovery workaround",
                    source_url=(
                        f"https://github.com/{repository}/issues/1#issuecomment-9001"
                    ),
                    source_created_at=BASE_TIME,
                    source_updated_at=BASE_TIME,
                )
            )

    embedding = FakeEmbeddingProvider()

    async def build():
        return await index_repository_history(
            session_factory,
            embedding,
            repository,
            [1, 2],
            embedding_provider_name=PROVIDER,
            embedding_model=MODEL,
            embedding_dimensions=DIMENSIONS,
        )

    first = await build()

    assert first.documents_found == 3
    assert first.documents_embedded == 3
    assert first.documents_reused == 0
    assert embedding.documents_embedded == 3

    second = await build()

    assert second.documents_found == 3
    assert second.documents_embedded == 0
    assert second.documents_reused == 3
    assert embedding.documents_embedded == 3

    async with session_factory() as session:
        hits = await search_repository_history_semantic(
            session,
            embedding,
            repository,
            "cancellation",
            embedding_provider_name=PROVIDER,
            embedding_model=MODEL,
            embedding_dimensions=DIMENSIONS,
            top_k=2,
        )

        assert len(hits) == 2
        assert {hit.source_type for hit in hits} == {"item", "comment"}
        assert all(hit.number == 1 for hit in hits)
        assert all(math.isclose(hit.distance, 0.0, abs_tol=1e-6) for hit in hits)

        comment_hit = next(hit for hit in hits if hit.source_type == "comment")
        assert comment_hit.comment_id == 9001
        assert comment_hit.source_url.endswith("#issuecomment-9001")

        wrong_model = await search_repository_history_semantic(
            session,
            embedding,
            repository,
            "cancellation",
            embedding_provider_name=PROVIDER,
            embedding_model="another-model",
            embedding_dimensions=DIMENSIONS,
        )
        assert wrong_model == ()

        wrong_repository = await search_repository_history_semantic(
            session,
            embedding,
            other_repository,
            "cancellation",
            embedding_provider_name=PROVIDER,
            embedding_model=MODEL,
            embedding_dimensions=DIMENSIONS,
        )
        assert wrong_repository == ()

    # Update the parent record and rebuild the affected evidence.
    updated_issue = _item(
        repository,
        1,
        "AsyncClient cancellation handling updated",
        updated_at=BASE_TIME + timedelta(days=1),
    )

    await _save_items(session_factory, repository, updated_issue)

    third = await build()

    # Parent and its comment both include the parent title in embedding text.
    assert third.documents_embedded == 2
    assert third.documents_reused == 1
    assert embedding.documents_embedded == 5

    async with session_factory() as session:
        saved = await session.get(
            RepositoryHistoryEmbedding,
            (
                repository,
                "item:1",
                PROVIDER,
                MODEL,
                DIMENSIONS,
            ),
        )

        count = await session.scalar(
            select(func.count())
            .select_from(RepositoryHistoryEmbedding)
            .where(RepositoryHistoryEmbedding.repository == repository)
        )

        assert saved is not None
        assert saved.title == updated_issue.title
        assert saved.source_updated_at == updated_issue.updated_at
        assert count == 3


async def test_invalid_vectors_are_rejected() -> None:
    with pytest.raises(ValueError):
        _validate_vector([0.0, 0.0, 0.0], 3)

    with pytest.raises(ValueError):
        _validate_vector([math.nan, 1.0, 0.0], 3)

    with pytest.raises(ValueError):
        _validate_vector([1.0, 0.0], 3)


async def test_invalid_index_request_does_not_call_embedding(
    history_db,
) -> None:
    repository, _, session_factory = history_db
    embedding = FakeEmbeddingProvider()

    with pytest.raises(ValueError):
        await index_repository_history(
            session_factory,
            embedding,
            repository,
            [1, 1],
            embedding_provider_name=PROVIDER,
            embedding_model=MODEL,
            embedding_dimensions=DIMENSIONS,
        )

    assert embedding.document_calls == 0
