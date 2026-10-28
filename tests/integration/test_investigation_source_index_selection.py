"""Explicit Source UUID validation and opt-in PostgreSQL metadata checks.

Default tests are isolated from settings, databases and providers. PostgreSQL
cases require RUN_POSTGRES_INTEGRATION=1 and use their own index IDs/cleanup.
No GitHub, LLM or paid embedding calls; no table creation or migrations.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import SQLAlchemyError

import maintainer_lens.agents.investigation.retrieval as retrieval
from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.services.investigation_source_index import (
    InvestigationSourceIndex,
    InvestigationSourceIndexNotReadyError,
    get_investigation_source_index,
)
from maintainer_lens.services.repository_vector_search import RepositoryIndexNotReady

pytestmark = pytest.mark.anyio
requires_postgres = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration tests are disabled",
)
INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _index(**changes):
    values = {
        "id": INDEX_ID,
        "repository": "encode/httpx",
        "commit_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "embedding_provider": "selection-provider",
        "embedding_model": "selection-model",
        "embedding_dimensions": 3,
        "chunker_version": "selected-chunker",
        "status": "ready",
    }
    values.update(changes)
    return RepositoryIndex(**values)


def _session(index):
    return SimpleNamespace(
        get=AsyncMock(return_value=index),
        begin=Mock(side_effect=AssertionError("Caller owns transactions")),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


async def _select(session, **changes):
    options = {
        "repository": "encode/httpx",
        "source_index_id": INDEX_ID,
        "embedding_provider_name": "selection-provider",
        "embedding_model": "selection-model",
        "embedding_dimensions": 3,
    }
    options.update(changes)
    return await get_investigation_source_index(session, **options)


async def test_explicit_index_returns_frozen_metadata_without_transaction_control():
    index = _index()
    session = _session(index)
    selected = await _select(session)
    assert selected == InvestigationSourceIndex(
        index_id=INDEX_ID,
        repository="encode/httpx",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        embedding_provider="selection-provider",
        embedding_model="selection-model",
        embedding_dimensions=3,
        chunker_version="selected-chunker",
    )
    session.get.assert_awaited_once_with(
        RepositoryIndex, INDEX_ID, populate_existing=True
    )
    session.begin.assert_not_called()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    index.commit_sha = "c" * 40
    assert selected.commit_sha == "a" * 40
    with pytest.raises(FrozenInstanceError):
        selected.commit_sha = "d" * 40


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", None),
        ("repository", ""),
        ("repository", "owner/../repo"),
        ("repository", "https://github.com/owner/repo"),
        ("source_index_id", str(INDEX_ID)),
        ("source_index_id", None),
        ("embedding_provider_name", " "),
        ("embedding_provider_name", True),
        ("embedding_model", ""),
        ("embedding_model", None),
        ("embedding_dimensions", True),
        ("embedding_dimensions", 0),
        ("embedding_dimensions", -1),
        ("embedding_dimensions", 3.0),
        ("embedding_dimensions", "3"),
        ("embedding_dimensions", None),
    ],
)
async def test_invalid_request_is_rejected_before_lookup(field, value):
    session = _session(_index())
    with pytest.raises(ValueError):
        await _select(session, **{field: value})
    session.get.assert_not_awaited()


async def test_missing_index_is_not_an_empty_success():
    session = _session(None)
    with pytest.raises(ValueError, match="does not exist"):
        await _select(session)
    session.get.assert_awaited_once()


async def test_foreign_repository_rejected_before_readiness_details():
    session = _session(_index(repository="other/repo", status="failed"))
    with pytest.raises(ValueError, match="does not belong"):
        await _select(session)


@pytest.mark.parametrize("status", ["building", "failed"])
async def test_not_ready_error_preserves_status_and_existing_error_family(status):
    session = _session(_index(status=status))
    with pytest.raises(InvestigationSourceIndexNotReadyError) as caught:
        await _select(session)
    assert isinstance(caught.value, RepositoryIndexNotReady)
    assert caught.value.index_id == INDEX_ID
    assert caught.value.status == status


@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_provider", "another-provider"),
        ("embedding_model", "another-model"),
        ("embedding_dimensions", 4),
    ],
)
async def test_embedding_identity_must_match_all_fields(field, value):
    session = _session(_index(**{field: value}))
    with pytest.raises(ValueError, match=field):
        await _select(session)


@pytest.mark.parametrize(
    "error",
    [
        SQLAlchemyError("Lookup failed"),
        ValueError("Dependency validation failed"),
        asyncio.CancelledError(),
        ExceptionGroup("Lookup group", [RuntimeError("Disconnected"), TimeoutError()]),
    ],
)
async def test_dependency_errors_and_cancellation_preserve_identity(error):
    session = _session(None)
    session.get.side_effect = error
    with pytest.raises(type(error)) as caught:
        await _select(session)
    assert caught.value is error
    session.rollback.assert_not_awaited()


@pytest.mark.parametrize("invalid", [False, True])
async def test_adapter_validates_before_retrieval_and_ends_lookup_transaction(
    monkeypatch, invalid
):
    index = _index(embedding_dimensions=4 if invalid else 3)
    active = False
    closed = []
    session = SimpleNamespace(get=AsyncMock(return_value=index))

    @asynccontextmanager
    async def transaction():
        nonlocal active
        assert not active
        active = True
        try:
            yield
        finally:
            active = False

    @asynccontextmanager
    async def factory():
        try:
            yield session
        finally:
            closed.append(True)

    session.begin = transaction
    embedding = SimpleNamespace(embed_query=AsyncMock())

    async def retrieve(*args, **kwargs):
        assert not active
        return []

    search = AsyncMock(side_effect=retrieve)
    monkeypatch.setattr(retrieval, "search_repository", search)
    source = retrieval.create_source_searcher(
        factory,
        embedding,
        embedding_provider_name="selection-provider",
        embedding_model="selection-model",
        embedding_dimensions=3,
    )
    if invalid:
        with pytest.raises(ValueError, match="embedding_dimensions"):
            await source("encode/httpx", INDEX_ID, "cleanup()")
        search.assert_not_awaited()
    else:
        assert await source("encode/httpx", INDEX_ID, "cleanup()") == []
        search.assert_awaited_once()
    embedding.embed_query.assert_not_awaited()
    assert not active and closed == [True]


@pytest.fixture
async def source_selection_context(session_factory):
    repository = f"source-selection/repo-{uuid4().hex}"
    ids = (uuid4(), uuid4())
    try:
        async with session_factory() as session:
            async with session.begin():
                session.add_all(
                    [
                        _index(id=ids[0], repository=repository),
                        _index(
                            id=ids[1],
                            repository=repository,
                            commit_sha="c" * 40,
                            chunker_version="other-chunker",
                        ),
                    ]
                )
        yield SimpleNamespace(factory=session_factory, repository=repository, ids=ids)
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(RepositoryIndex).where(RepositoryIndex.id.in_(ids))
                )


async def test_adapter_lookup_cancellation_releases_transaction_and_session(
    monkeypatch,
):
    started = asyncio.Event()
    cleaned = []

    async def lookup(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    @asynccontextmanager
    async def transaction():
        try:
            yield
        finally:
            cleaned.append("transaction")

    @asynccontextmanager
    async def factory():
        try:
            yield SimpleNamespace(get=AsyncMock(side_effect=lookup), begin=transaction)
        finally:
            cleaned.append("session")

    embedding = SimpleNamespace(embed_query=AsyncMock())
    downstream = AsyncMock()
    monkeypatch.setattr(retrieval, "search_repository", downstream)
    source = retrieval.create_source_searcher(
        factory,
        embedding,
        embedding_provider_name="selection-provider",
        embedding_model="selection-model",
        embedding_dimensions=3,
    )
    task = asyncio.create_task(source("encode/httpx", INDEX_ID, "cleanup()"))
    try:
        async with asyncio.timeout(3):
            await started.wait()
        task.cancel("stop preflight")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ("stop preflight",)
        assert cleaned == ["transaction", "session"]
        downstream.assert_not_awaited()
        embedding.embed_query.assert_not_awaited()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _read(context, session, index_id=None, **changes):
    return await _select(
        session,
        repository=context.repository,
        source_index_id=context.ids[0] if index_id is None else index_id,
        **changes,
    )


@requires_postgres
async def test_postgres_selected_uuid_pins_revision_and_chunker(
    source_selection_context,
):
    context = source_selection_context
    async with context.factory() as session:
        async with session.begin():
            first = await _read(context, session)
            other = await _read(context, session, context.ids[1])
    assert first.index_id == context.ids[0] and first.commit_sha == "a" * 40
    assert other.index_id == context.ids[1] and other.commit_sha == "c" * 40
    assert first.chunker_version == "selected-chunker"
    assert other.chunker_version == "other-chunker"
    # Dataclasses remain usable after both transaction and Session have ended.
    assert first.embedding_dimensions == 3


@requires_postgres
@pytest.mark.parametrize("status", ["building", "failed"])
async def test_postgres_never_falls_back_to_another_ready_index(
    source_selection_context, status
):
    context = source_selection_context
    async with context.factory() as session:
        async with session.begin():
            await session.execute(
                update(RepositoryIndex)
                .where(RepositoryIndex.id == context.ids[0])
                .values(status=status)
            )
    async with context.factory() as session:
        async with session.begin():
            with pytest.raises(InvestigationSourceIndexNotReadyError) as caught:
                await _read(context, session)
    assert caught.value.index_id == context.ids[0]
    assert caught.value.status == status


@requires_postgres
async def test_postgres_refreshes_cached_index_before_validation(
    source_selection_context,
):
    context = source_selection_context
    async with context.factory() as session:
        async with session.begin():
            cached = await session.get(RepositoryIndex, context.ids[0])
            await _read(context, session)
        async with context.factory() as writer:
            async with writer.begin():
                await writer.execute(
                    update(RepositoryIndex)
                    .where(RepositoryIndex.id == context.ids[0])
                    .values(embedding_dimensions=4)
                )
        assert cached.embedding_dimensions == 3
        async with session.begin():
            with pytest.raises(ValueError, match="embedding_dimensions"):
                await _read(context, session)
        assert cached.embedding_dimensions == 4


@requires_postgres
async def test_postgres_metadata_read_does_not_commit_caller_changes(
    source_selection_context,
):
    context = source_selection_context
    async with context.factory() as session:
        async with session.begin():
            await session.execute(
                update(RepositoryIndex)
                .where(RepositoryIndex.id == context.ids[0])
                .values(chunker_version="temporary")
            )
            selected = await _read(context, session)
            assert selected.chunker_version == "temporary"
            await session.rollback()
    async with context.factory() as session:
        async with session.begin():
            assert (await _read(context, session)).chunker_version == "selected-chunker"


@requires_postgres
async def test_postgres_ready_empty_index_is_a_valid_empty_search(
    source_selection_context,
):
    context = source_selection_context

    class Embedding:
        calls = 0

        async def embed_query(self, query):
            self.calls += 1
            return [1.0, 0.0, 0.0]

    embedding = Embedding()
    source = retrieval.create_source_searcher(
        context.factory,
        embedding,
        embedding_provider_name="selection-provider",
        embedding_model="selection-model",
        embedding_dimensions=3,
    )
    assert await source(context.repository, context.ids[0], "cleanup()") == []
    assert embedding.calls == 1
