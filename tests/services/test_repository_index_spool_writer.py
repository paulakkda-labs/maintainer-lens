"""Replayable local-spool writer tests without database/network resources."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import RepositoryFile, RepositoryRevision
from maintainer_lens.services.repository_index_writer import (
    EmbeddedChunk,
    write_repository_index,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _item(number, *, vector=None):
    return EmbeddedChunk(
        CodeChunk(
            file=RepositoryFile(
                path=f"file{number}.py", blob_sha="a" * 40, size=4, mode="100644"
            ),
            language="python",
            kind="file",
            start_line=1,
            end_line=1,
            content="x=1\n",
        ),
        [1.0, 0.0, 0.0] if vector is None else vector,
    )


def _session():
    events, records = [], []

    @asynccontextmanager
    async def begin():
        events.append("begin")
        try:
            yield
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    session = SimpleNamespace(
        begin=begin,
        add_all=lambda batch: records.append(batch),
        flush=AsyncMock(),
        execute=AsyncMock(
            side_effect=[Mock(scalar_one_or_none=lambda: uuid4()), Mock()]
        ),
    )
    return session, events, records


async def _write(session, replay):
    return await write_repository_index(
        session,
        RepositoryRevision(
            repository="org/repo",
            default_branch="main",
            commit_sha="a" * 40,
            tree_sha="b" * 40,
        ),
        replay,
        embedding_provider="provider",
        embedding_model="model",
        embedding_dimensions=3,
        chunker_version="version",
    )


async def test_spool_is_validated_then_written_in_bounded_batches():
    session, events, records = _session()
    replays = []

    def replay():
        replays.append(len(replays))
        yield from (_item(n) for n in range(53))

    await _write(session, replay)
    assert len(replays) == 2 and events == ["begin", "commit"]
    assert list(map(len, records)) == [50, 3] and session.flush.await_count == 2
    assert len({record.chunk_key for batch in records for record in batch}) == 53


@pytest.mark.parametrize(
    "items",
    [
        [],
        [_item(1), _item(1)],
        [_item(1, vector=[float("nan"), 0, 0])],
        [_item(1, vector=[1])],
    ],
)
async def test_spool_validation_fails_before_sql(items):
    session, events, _ = _session()
    with pytest.raises(ValueError):
        await _write(session, lambda: iter(items))
    assert not events
    session.execute.assert_not_awaited()


async def test_changed_spool_rolls_back_before_ready_publication():
    session, events, _ = _session()
    runs = iter(([_item(1)], [_item(2)]))
    with pytest.raises(ValueError, match="changed"):
        await _write(session, lambda: iter(next(runs)))
    assert events == ["begin", "rollback"]
    assert session.execute.await_count == 1
