"""Integration tests for transactional repository index persistence."""

import os
from secrets import token_hex

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from maintainer_lens.core.config import get_settings
from maintainer_lens.db.models.repository_index import (
    RepositoryChunk,
    RepositoryIndex,
)
from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositoryRevision,
)
from maintainer_lens.services.repository_index_writer import (
    EmbeddedChunk,
    RepositoryIndexConflict,
    write_repository_index,
)


def make_revision() -> RepositoryRevision:
    """Create a unique repository revision for each test run."""

    return RepositoryRevision(
        repository="pypa/sampleproject",
        default_branch="main",
        commit_sha=token_hex(20),
        tree_sha=token_hex(20),
    )


def make_chunk(
    *,
    content: str = "def add_one(number):\n    return number + 1\n",
    language: str = "python",
) -> CodeChunk:
    """Create a source chunk with realistic location metadata."""

    return CodeChunk(
        file=RepositoryFile(
            path="src/sample.py",
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        language=language,
        kind="function",
        symbol="add_one",
        symbols=["add_one"],
        start_line=1,
        end_line=2,
        content=content,
    )


@pytest.mark.anyio
async def test_repository_index_transaction_and_idempotency() -> None:
    """Verify atomic writes, replay safety, and conflict detection."""

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration tests are disabled")

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )

    revision = make_revision()
    item = EmbeddedChunk(make_chunk(), [0.1, 0.2, 0.3])

    options = {
        "embedding_provider": "test-provider",
        "embedding_model": "test-model",
        "embedding_dimensions": 3,
        "chunker_version": "test-v1",
    }

    try:
        async with engine.connect() as connection:
            outer_transaction = await connection.begin()

            async def write(
                target_revision: RepositoryRevision,
                embedded_items: list[EmbeddedChunk],
            ):
                async with AsyncSession(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    return await write_repository_index(
                        session,
                        target_revision,
                        embedded_items,
                        **options,
                    )

            try:
                # A complete write becomes ready.
                index_id = await write(revision, [item])

                status = await connection.scalar(
                    select(RepositoryIndex.status).where(RepositoryIndex.id == index_id)
                )

                assert status == "ready"

                chunk_count = await connection.scalar(
                    select(func.count())
                    .select_from(RepositoryChunk)
                    .where(RepositoryChunk.index_id == index_id)
                )

                assert chunk_count == 1

                # Replaying identical data reuses the same index.
                replayed_id = await write(revision, [item])

                assert replayed_id == index_id

                chunk_count = await connection.scalar(
                    select(func.count())
                    .select_from(RepositoryChunk)
                    .where(RepositoryChunk.index_id == index_id)
                )

                assert chunk_count == 1

                # A different chunk set cannot reuse the same identity.
                changed = EmbeddedChunk(
                    make_chunk(
                        content=("def add_one(number):\n    return number + 2\n")
                    ),
                    [0.1, 0.2, 0.3],
                )

                with pytest.raises(
                    RepositoryIndexConflict,
                    match="different chunks",
                ):
                    await write(revision, [changed])

                # Force a database-level failure after index insertion.
                bad_revision = make_revision()
                invalid_chunk = EmbeddedChunk(
                    make_chunk(language="x" * 65),
                    [0.1, 0.2, 0.3],
                )

                with pytest.raises(SQLAlchemyError):
                    await write(bad_revision, [invalid_chunk])

                # The failed transaction leaves no index record.
                failed_count = await connection.scalar(
                    select(func.count())
                    .select_from(RepositoryIndex)
                    .where(RepositoryIndex.commit_sha == bad_revision.commit_sha)
                )

                assert failed_count == 0

            finally:
                # Never persist integration-test records.
                await outer_transaction.rollback()

    finally:
        await engine.dispose()
