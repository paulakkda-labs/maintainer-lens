"""PostgreSQL integration tests for repository keyword search."""

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
from maintainer_lens.services.repository_keyword_search import (
    search_repository_by_keyword,
)

REPOSITORY = "pypa/sampleproject"


def make_revision() -> RepositoryRevision:
    """Create an isolated repository revision for testing."""

    return RepositoryRevision(
        repository=REPOSITORY,
        default_branch="main",
        commit_sha=token_hex(20),
        tree_sha=token_hex(20),
    )


def make_item(
    path: str,
    symbol: str,
    content: str,
) -> EmbeddedChunk:
    """Create a source chunk and a valid synthetic embedding."""

    source_file = RepositoryFile(
        path=path,
        blob_sha="a" * 40,
        size=len(content.encode("utf-8")),
        mode="100644",
    )

    chunk = CodeChunk(
        file=source_file,
        language="python",
        kind="function",
        symbol=symbol,
        symbols=[symbol],
        start_line=1,
        end_line=max(1, len(content.splitlines())),
        content=content,
    )

    return EmbeddedChunk(
        chunk=chunk,
        vector=[0.1, 0.2, 0.3],
    )


@pytest.mark.anyio
async def test_keyword_search_priority_isolation_and_literal_matching() -> None:
    """Rank identifier matches and isolate repository indexes."""

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

    items = [
        make_item(
            "src/handlers.py",
            "CancelledError",
            "def CancelledError():\n    return None\n",
        ),
        make_item(
            "src/CancelledError.py",
            "handler",
            "def handler():\n    return None\n",
        ),
        make_item(
            "src/worker.py",
            "worker",
            "raise asyncio.CancelledError()\n",
        ),
        make_item(
            "src/other.py",
            "other",
            "print('done')\n",
        ),
        make_item(
            "src/literal.py",
            "literal",
            "metric = 'user_id%'\n",
        ),
        make_item(
            "src/decoy.py",
            "decoy",
            "metric = 'userXidX'\n",
        ),
    ]

    try:
        async with session_factory() as session:
            index_id = await write_repository_index(
                session,
                revision,
                items,
                embedding_provider="test-provider",
                embedding_model="test-model",
                embedding_dimensions=3,
                chunker_version="keyword-test-v1",
            )

        async with session_factory() as session:
            other_index_id = await write_repository_index(
                session,
                other_revision,
                [
                    make_item(
                        "src/foreign.py",
                        "CancelledError",
                        "def CancelledError(): pass\n",
                    )
                ],
                embedding_provider="test-provider",
                embedding_model="test-model",
                embedding_dimensions=3,
                chunker_version="keyword-test-v1",
            )

        assert index_id != other_index_id

        async with session_factory() as session:
            results = await search_repository_by_keyword(
                session,
                index_id,
                "CancelledError",
                top_k=10,
            )

        assert [item.file_path for item in results] == [
            "src/handlers.py",
            "src/CancelledError.py",
            "src/worker.py",
        ]

        assert [item.matched_in for item in results] == [
            "symbol",
            "file_path",
            "content",
        ]

        for item in results:
            assert item.index_id == index_id
            assert item.repository == revision.repository
            assert item.commit_sha == revision.commit_sha
            assert item.chunk_key
            assert item.start_line <= item.end_line

        async with session_factory() as session:
            top_two = await search_repository_by_keyword(
                session,
                index_id,
                "CancelledError",
                top_k=2,
            )

        assert len(top_two) == 2

        assert [item.file_path for item in top_two] == [
            "src/handlers.py",
            "src/CancelledError.py",
        ]

        # Both _ and % must be treated as literal characters.
        async with session_factory() as session:
            literal_results = await search_repository_by_keyword(
                session,
                index_id,
                "user_id%",
            )

        assert [item.file_path for item in literal_results] == ["src/literal.py"]

        # A nonmatching keyword should produce an empty result.
        async with session_factory() as session:
            missing_results = await search_repository_by_keyword(
                session,
                index_id,
                "nonexistent_identifier_12345",
            )

        assert missing_results == []

    finally:
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
