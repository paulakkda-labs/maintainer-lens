"""Test PostgreSQL evidence-anchor resolution with disposable data."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.models.repository_index import (
    RepositoryChunk,
    RepositoryIndex,
)
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.evaluation.retrieval_anchor_resolution import (
    resolve_dataset_chunk_keys,
)
from maintainer_lens.evaluation.retrieval_dataset import (
    load_retrieval_dataset,
)

DATASET_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "retrieval"
    / "sampleproject.json"
)

ADD_ONE_KEY = "a" * 64
ENTRY_POINT_KEY = "b" * 64


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_resolves_ground_truth_against_real_postgres() -> None:
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration test is disabled")

    dataset = load_retrieval_dataset(DATASET_PATH)
    index_id = uuid4()

    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())
    session_factory = create_session_factory(engine)

    try:
        # Arrange: create isolated, committed source records.
        async with session_factory() as session:
            async with session.begin():
                session.add(
                    RepositoryIndex(
                        id=index_id,
                        repository=dataset.repository,
                        commit_sha=dataset.commit_sha,
                        tree_sha="c" * 40,
                        embedding_provider="integration-test",
                        embedding_model=f"anchor-resolution-{index_id.hex}",
                        embedding_dimensions=3,
                        chunker_version="test-v1",
                        status="ready",
                    )
                )

                await session.flush()

                session.add_all(
                    [
                        RepositoryChunk(
                            id=uuid4(),
                            index_id=index_id,
                            chunk_key=ADD_ONE_KEY,
                            file_path="src/sample/simple.py",
                            blob_sha="d" * 40,
                            language="python",
                            kind="function",
                            symbol="add_one",
                            symbols=["add_one"],
                            heading_path=[],
                            headings=[],
                            start_line=1,
                            end_line=2,
                            content=("def add_one(number):\n    return number + 1\n"),
                            embedding=[1.0, 0.0, 0.0],
                        ),
                        RepositoryChunk(
                            id=uuid4(),
                            index_id=index_id,
                            chunk_key=ENTRY_POINT_KEY,
                            file_path="src/sample/__init__.py",
                            blob_sha="e" * 40,
                            language="python",
                            kind="function",
                            symbol="main",
                            symbols=["main"],
                            heading_path=[],
                            headings=[],
                            start_line=1,
                            end_line=3,
                            content=(
                                "def main():\n"
                                '    """Entry point for the application script"""\n'
                                '    print("Call your main application code here")\n'
                            ),
                            embedding=[0.0, 1.0, 0.0],
                        ),
                    ]
                )

        # Act: read committed rows through a separate session.
        async with session_factory() as session:
            resolved = await resolve_dataset_chunk_keys(
                session,
                index_id,
                dataset,
            )

        # Assert: all queries resolve to their manually labeled source.
        expected = {
            "add-one-en": frozenset({ADD_ONE_KEY}),
            "add-one-zh": frozenset({ADD_ONE_KEY}),
            "add-one-identifier-en": frozenset({ADD_ONE_KEY}),
            "add-one-identifier-zh": frozenset({ADD_ONE_KEY}),
            "entry-point-en": frozenset({ENTRY_POINT_KEY}),
            "entry-point-zh": frozenset({ENTRY_POINT_KEY}),
            "entry-point-identifier-en": frozenset({ENTRY_POINT_KEY}),
            "entry-point-identifier-zh": frozenset({ENTRY_POINT_KEY}),
        }

        assert resolved == expected

    finally:
        try:
            # Remove only the records created by this test.
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        delete(RepositoryChunk).where(
                            RepositoryChunk.index_id == index_id
                        )
                    )
                    await session.execute(
                        delete(RepositoryIndex).where(RepositoryIndex.id == index_id)
                    )

            # Verify that the test has left no matching records.
            async with session_factory() as session:
                remaining_index = await session.scalar(
                    select(RepositoryIndex.id).where(RepositoryIndex.id == index_id)
                )
                remaining_chunk = await session.scalar(
                    select(RepositoryChunk.id).where(
                        RepositoryChunk.index_id == index_id
                    )
                )

                assert remaining_index is None
                assert remaining_chunk is None

        finally:
            await engine.dispose()
