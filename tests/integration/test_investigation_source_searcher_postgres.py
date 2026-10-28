"""Exercise the graph source adapter against real PostgreSQL and pgvector.

Only embeddings are fake. These tests create isolated indexes and remove them
on teardown; they never call GitHub or an embedding provider.
"""

import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.agents.investigation.retrieval import create_source_searcher
from maintainer_lens.agents.investigation_graph import build_investigation_graph
from maintainer_lens.db.models.repository_index import RepositoryChunk, RepositoryIndex

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def source_context(db_engine, session_factory):
    repository = f"source-regression/repo-{uuid4().hex}"
    index_id = uuid4()
    sessions = []

    class ObservedSession(AsyncSession):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.was_closed = False
            sessions.append(self)

        async def close(self):
            try:
                await super().close()
            finally:
                self.was_closed = True

    observed_factory = async_sessionmaker(
        db_engine, class_=ObservedSession, expire_on_commit=False
    )

    try:
        async with session_factory.begin() as session:
            session.add(
                RepositoryIndex(
                    id=index_id,
                    repository=repository,
                    commit_sha="a" * 40,
                    tree_sha="b" * 40,
                    embedding_provider="regression-provider",
                    embedding_model="regression-model",
                    embedding_dimensions=3,
                    chunker_version="source-adapter-regression",
                    status="ready",
                )
            )
            await session.flush()
            session.add(
                RepositoryChunk(
                    index_id=index_id,
                    chunk_key="c" * 64,
                    file_path="src/cleanup.py",
                    blob_sha="d" * 40,
                    language="python",
                    kind="function",
                    symbol="cleanup",
                    start_line=1,
                    end_line=2,
                    content="def cleanup():\n    return True\n",
                    embedding=[1.0, 0.0, 0.0],
                )
            )
        yield SimpleNamespace(
            repository=repository,
            index_id=index_id,
            factory=observed_factory,
            sessions=sessions,
            setup_factory=session_factory,
        )
    finally:
        async with session_factory.begin() as session:
            await session.execute(
                delete(RepositoryIndex).where(RepositoryIndex.id == index_id)
            )


class ProbeEmbedding:
    def __init__(self, context, *, error=None, blocked=False):
        self.context = context
        self.error = error
        self.blocked = blocked
        self.started = asyncio.Event()
        self.calls = 0

    async def embed_query(self, query):
        self.calls += 1
        assert query
        assert self.context.sessions
        assert all(not session.in_transaction() for session in self.context.sessions)
        self.started.set()
        if self.blocked:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return [1.0, 0.0, 0.0]


def make_searcher(context, embedding):
    return create_source_searcher(
        context.factory,
        embedding,
        embedding_provider_name="regression-provider",
        embedding_model="regression-model",
    )


def assert_sessions_closed(context):
    assert context.sessions
    assert all(session.was_closed for session in context.sessions)
    assert all(not session.in_transaction() for session in context.sessions)


async def test_real_graph_source_adapter_preserves_transaction_boundaries(
    source_context,
):
    context = source_context
    embedding = ProbeEmbedding(context)

    async def no_history(repository, query):
        return ()

    graph = build_investigation_graph(
        no_history, source_searcher=make_searcher(context, embedding)
    )
    result = await graph.ainvoke(
        {
            "repository": context.repository,
            "issue_number": 42,
            "issue_title": "Where is cleanup() defined?",
            "issue_body": "Investigate the cleanup implementation.",
            "source_index_id": str(context.index_id),
        }
    )

    assert embedding.calls == 1
    evidence = result["source_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["index_id"] == str(context.index_id)
    assert evidence[0]["chunk_key"] == "c" * 64
    assert evidence[0]["vector_rank"] == 1
    assert evidence[0]["keyword_rank"] == 1
    assert evidence[0]["source_url"].endswith("/src/cleanup.py#L1-L2")
    assert_sessions_closed(context)


@pytest.mark.parametrize("invalid", ["missing", "foreign", "not_ready", "model"])
async def test_invalid_index_is_rejected_before_embedding(source_context, invalid):
    context = source_context
    repository = context.repository
    index_id = context.index_id
    if invalid == "missing":
        index_id = uuid4()
    elif invalid == "foreign":
        repository = "another/repository"
    else:
        values = (
            {"status": "building"}
            if invalid == "not_ready"
            else {"embedding_model": "different-model"}
        )
        async with context.setup_factory.begin() as session:
            await session.execute(
                update(RepositoryIndex)
                .where(RepositoryIndex.id == index_id)
                .values(**values)
            )

    embedding = ProbeEmbedding(context)
    with pytest.raises((ValueError, RuntimeError)):
        await make_searcher(context, embedding)(repository, index_id, "cleanup()")
    assert embedding.calls == 0
    assert_sessions_closed(context)


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
async def test_embedding_failure_closes_session_and_allows_retry(
    source_context, error_type
):
    context = source_context
    error = error_type("Simulated embedding failure")
    embedding = ProbeEmbedding(context, error=error)
    search = make_searcher(context, embedding)
    with pytest.raises(error_type) as captured:
        await search(context.repository, context.index_id, "cleanup()")
    assert captured.value is error
    assert_sessions_closed(context)

    embedding.error = None
    hits = await search(context.repository, context.index_id, "cleanup()")
    assert len(hits) == 1
    assert embedding.calls == 2
    assert_sessions_closed(context)


async def test_cancellation_propagates_and_closes_source_session(source_context):
    context = source_context
    embedding = ProbeEmbedding(context, blocked=True)
    search = make_searcher(context, embedding)
    task = asyncio.create_task(
        search(context.repository, context.index_id, "cleanup()")
    )
    try:
        await asyncio.wait_for(embedding.started.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert_sessions_closed(context)
