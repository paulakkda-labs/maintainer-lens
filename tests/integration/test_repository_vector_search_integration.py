"""PostgreSQL integration tests for repository vector search."""

import hashlib
import os
from secrets import token_hex

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maintainer_lens.core.config import get_settings
from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositoryRevision,
)
from maintainer_lens.services.repository_index_writer import (
    EmbeddedChunk,
    write_repository_index,
)
from maintainer_lens.services.repository_vector_search import (
    search_repository_chunks,
)

REPOSITORY = "pypa/sampleproject"
EMBEDDING_PROVIDER = "test-provider"
EMBEDDING_MODEL = "test-model"
CHUNKER_VERSION = "retrieval-test-v1"


def make_revision() -> RepositoryRevision:
    """Create a unique repository revision for integration testing."""

    return RepositoryRevision(
        repository=REPOSITORY,
        default_branch="main",
        commit_sha=token_hex(20),
        tree_sha=token_hex(20),
    )


def make_item(name: str, vector: list[float]) -> EmbeddedChunk:
    """Create a source chunk with a deterministic Git blob identity."""

    content = f"def {name}():\n    return '{name}'\n"
    encoded = content.encode("utf-8")

    header = f"blob {len(encoded)}\0".encode("ascii")
    blob_sha = hashlib.sha1(header + encoded).hexdigest()

    source_file = RepositoryFile(
        path=f"src/{name}.py",
        blob_sha=blob_sha,
        size=len(encoded),
        mode="100644",
    )

    chunk = CodeChunk(
        file=source_file,
        language="python",
        kind="function",
        symbol=name,
        symbols=[name],
        start_line=1,
        end_line=2,
        content=content,
    )

    return EmbeddedChunk(
        chunk=chunk,
        vector=vector,
    )


@pytest.mark.anyio
async def test_vector_search_ranking_and_index_isolation() -> None:
    """Rank nearest chunks and never mix repository index identities."""

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration tests are disabled")

    settings = get_settings()

    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )

    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )

    revision = make_revision()
    other_revision = make_revision()

    try:
        # Three known vectors belonging to the requested index.
        items = [
            make_item("near", [1.0, 0.0, 0.0]),
            make_item("medium", [0.8, 0.6, 0.0]),
            make_item("far", [0.0, 1.0, 0.0]),
        ]

        async with session_factory() as session:
            index_id = await write_repository_index(
                session,
                revision,
                items,
                embedding_provider=EMBEDDING_PROVIDER,
                embedding_model=EMBEDDING_MODEL,
                embedding_dimensions=3,
                chunker_version=CHUNKER_VERSION,
            )

        # A separate ready index uses a different vector dimension.
        # Its chunks must not appear in the requested index's results.
        async with session_factory() as session:
            other_index_id = await write_repository_index(
                session,
                other_revision,
                [make_item("unrelated", [1.0, 0.0])],
                embedding_provider=EMBEDDING_PROVIDER,
                embedding_model=EMBEDDING_MODEL,
                embedding_dimensions=2,
                chunker_version=CHUNKER_VERSION,
            )

        assert index_id != other_index_id

        # Retrieve all chunks from the requested index.
        async with session_factory() as session:
            results = await search_repository_chunks(
                session,
                index_id,
                [1.0, 0.0, 0.0],
                top_k=10,
            )

        assert len(results) == 3

        assert [result.file_path for result in results] == [
            "src/near.py",
            "src/medium.py",
            "src/far.py",
        ]

        assert results[0].distance == pytest.approx(0.0, abs=1e-6)
        assert results[1].distance == pytest.approx(0.2, abs=1e-6)
        assert results[2].distance == pytest.approx(1.0, abs=1e-6)

        # All returned evidence must belong to the requested index.
        for result in results:
            assert result.index_id == index_id
            assert result.repository == revision.repository
            assert result.commit_sha == revision.commit_sha
            assert result.start_line == 1
            assert result.end_line == 2
            assert result.chunk_key
            assert result.content

        # Top-K must be applied in PostgreSQL.
        async with session_factory() as session:
            top_two = await search_repository_chunks(
                session,
                index_id,
                [1.0, 0.0, 0.0],
                top_k=2,
            )

        assert len(top_two) == 2
        assert [result.file_path for result in top_two] == [
            "src/near.py",
            "src/medium.py",
        ]

    finally:
        # Delete only the synthetic revisions created by this test.
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    delete(RepositoryIndex).where(
                        RepositoryIndex.repository == REPOSITORY,
                        RepositoryIndex.commit_sha.in_(
                            [
                                revision.commit_sha,
                                other_revision.commit_sha,
                            ]
                        ),
                    )
                )
        finally:
            await engine.dispose()
