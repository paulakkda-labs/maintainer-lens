"""PostgreSQL integration tests for fenced repository index writes."""

import asyncio
import os
from secrets import token_hex
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from maintainer_lens.core.config import get_settings
from maintainer_lens.db.models.repository_index import (
    RepositoryChunk,
    RepositoryIndex,
)
from maintainer_lens.db.models.repository_index_lease import (
    RepositoryIndexBuildLease,
)
from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositoryRevision,
)
from maintainer_lens.services import repository_index_writer
from maintainer_lens.services.repository_index_lease import (
    RepositoryIndexLeaseLost,
    make_repository_index_identity_key,
    try_acquire_repository_index_lease,
)
from maintainer_lens.services.repository_index_writer import (
    EmbeddedChunk,
    write_repository_index,
)


def make_revision() -> RepositoryRevision:
    """Create a unique repository revision for each test."""

    return RepositoryRevision(
        repository="pypa/sampleproject",
        default_branch="main",
        commit_sha=token_hex(20),
        tree_sha=token_hex(20),
    )


def make_item() -> EmbeddedChunk:
    """Create one source chunk with a valid embedding."""

    content = "def add_one(value):\n    return value + 1\n"

    chunk = CodeChunk(
        file=RepositoryFile(
            path="src/sample.py",
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        language="python",
        kind="function",
        symbol="add_one",
        symbols=["add_one"],
        start_line=1,
        end_line=2,
        content=content,
    )

    return EmbeddedChunk(
        chunk=chunk,
        vector=[0.1, 0.2, 0.3],
    )


def identity_key(revision: RepositoryRevision) -> str:
    """Compute the lease identity for the test index configuration."""

    return make_repository_index_identity_key(
        revision,
        embedding_provider_name="test-provider",
        embedding_model="test-model",
        embedding_dimensions=3,
        chunker_version="test-v1",
    )


async def acquire(
    factory: async_sessionmaker[AsyncSession],
    revision: RepositoryRevision,
) -> UUID:
    """Acquire a repository build lease."""

    async with factory() as session:
        token = await try_acquire_repository_index_lease(
            session,
            revision,
            embedding_provider_name="test-provider",
            embedding_model="test-model",
            embedding_dimensions=3,
            chunker_version="test-v1",
        )

    assert isinstance(token, UUID)
    return token


async def write(
    factory: async_sessionmaker[AsyncSession],
    revision: RepositoryRevision,
    token: UUID,
) -> UUID:
    """Persist an index using a supplied owner token."""

    async with factory() as session:
        return await write_repository_index(
            session,
            revision,
            [make_item()],
            embedding_provider="test-provider",
            embedding_model="test-model",
            embedding_dimensions=3,
            chunker_version="test-v1",
            owner_token=token,
        )


async def cleanup(
    engine,
    revisions: list[RepositoryRevision],
) -> None:
    """Remove only the records created by these tests."""

    async with engine.begin() as connection:
        for revision in revisions:
            await connection.execute(
                delete(RepositoryIndex).where(
                    RepositoryIndex.repository == revision.repository,
                    RepositoryIndex.commit_sha == revision.commit_sha,
                )
            )

            await connection.execute(
                delete(RepositoryIndexBuildLease).where(
                    RepositoryIndexBuildLease.identity_key == identity_key(revision)
                )
            )


@pytest.mark.anyio
async def test_writer_rejects_stale_and_wrong_identity_tokens() -> None:
    """Reject lost ownership before inserting index records."""

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration tests are disabled")

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    first_revision = make_revision()
    second_revision = make_revision()

    try:
        first_token = await acquire(factory, first_revision)
        first_index_id = await write(
            factory,
            first_revision,
            first_token,
        )

        async with factory() as session:
            first_index = await session.get(
                RepositoryIndex,
                first_index_id,
            )
            assert first_index is not None
            assert first_index.status == "ready"

            chunk_count = await session.scalar(
                select(func.count())
                .select_from(RepositoryChunk)
                .where(RepositoryChunk.index_id == first_index_id)
            )
            assert chunk_count == 1

        stale_token = await acquire(factory, second_revision)

        async with engine.begin() as connection:
            await connection.execute(
                update(RepositoryIndexBuildLease)
                .where(
                    RepositoryIndexBuildLease.identity_key
                    == identity_key(second_revision)
                )
                .values(
                    expires_at=(func.clock_timestamp() - text("INTERVAL '1 second'"))
                )
            )

        replacement_token = await acquire(factory, second_revision)
        assert replacement_token != stale_token

        with pytest.raises(RepositoryIndexLeaseLost):
            await write(factory, second_revision, stale_token)

        with pytest.raises(RepositoryIndexLeaseLost):
            await write(factory, second_revision, first_token)

        async with factory() as session:
            failed_count = await session.scalar(
                select(func.count())
                .select_from(RepositoryIndex)
                .where(
                    RepositoryIndex.repository == second_revision.repository,
                    RepositoryIndex.commit_sha == second_revision.commit_sha,
                )
            )

            assert failed_count == 0

        second_index_id = await write(
            factory,
            second_revision,
            replacement_token,
        )

        async with factory() as session:
            second_index = await session.get(
                RepositoryIndex,
                second_index_id,
            )

            assert second_index is not None
            assert second_index.status == "ready"

    finally:
        try:
            await cleanup(
                engine,
                [first_revision, second_revision],
            )
        finally:
            await engine.dispose()


@pytest.mark.anyio
async def test_writer_holds_lease_lock_until_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent another connection from modifying a fenced lease."""

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration tests are disabled")

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    revision = make_revision()

    try:
        token = await acquire(factory, revision)

        locked = asyncio.Event()
        resume = asyncio.Event()

        original_guard = repository_index_writer.assert_repository_index_lease_owned

        async def paused_guard(
            session: AsyncSession,
            *,
            identity_key: str,
            owner_token: UUID,
        ) -> None:
            await original_guard(
                session,
                identity_key=identity_key,
                owner_token=owner_token,
            )

            locked.set()
            await resume.wait()

        monkeypatch.setattr(
            repository_index_writer,
            "assert_repository_index_lease_owned",
            paused_guard,
        )

        writer_task = asyncio.create_task(write(factory, revision, token))

        try:
            await asyncio.wait_for(locked.wait(), timeout=5)

            with pytest.raises(DBAPIError, match="lock timeout"):
                async with engine.begin() as connection:
                    await connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    await connection.execute(
                        update(RepositoryIndexBuildLease)
                        .where(
                            RepositoryIndexBuildLease.identity_key
                            == identity_key(revision)
                        )
                        .values(owner_token=uuid4())
                    )

        finally:
            resume.set()
            index_id = await writer_task

        async with factory() as session:
            index = await session.get(
                RepositoryIndex,
                index_id,
            )

            current_owner = await session.scalar(
                select(RepositoryIndexBuildLease.owner_token).where(
                    RepositoryIndexBuildLease.identity_key == identity_key(revision)
                )
            )

            assert index is not None
            assert index.status == "ready"
            assert current_owner == token

    finally:
        try:
            await cleanup(engine, [revision])
        finally:
            await engine.dispose()
