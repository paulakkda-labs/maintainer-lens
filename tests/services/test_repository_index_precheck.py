"""Tests for repository index preflight lookup and reuse."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from maintainer_lens.schemas.repository import RepositoryRevision
from maintainer_lens.services.repository_index_writer import (
    RepositoryIndexConflict,
)
from maintainer_lens.services.repository_indexing import (
    _find_existing_index,
    build_repository_index,
)


def make_revision() -> RepositoryRevision:
    """Build a fixed revision for repository index tests."""

    return RepositoryRevision(
        repository="pypa/sampleproject",
        default_branch="main",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
    )


def make_session(existing: object | None) -> MagicMock:
    """Mock the database lookup and transaction context."""

    session = MagicMock()
    session.scalars = AsyncMock()

    result = MagicMock()
    result.one_or_none.return_value = existing
    session.scalars.return_value = result

    return session


async def lookup(session: MagicMock):
    """Run an index lookup using a consistent configuration."""

    return await _find_existing_index(
        session,
        make_revision(),
        embedding_provider_name="dashscope",
        embedding_model="test-model",
        embedding_dimensions=1024,
        chunker_version="v1",
    )


@pytest.mark.anyio
async def test_precheck_returns_none_when_index_is_missing() -> None:
    """Continue indexing when no matching index exists."""

    session = make_session(None)

    result = await lookup(session)

    assert result is None
    session.scalars.assert_awaited_once()
    session.begin.assert_called_once()


@pytest.mark.anyio
async def test_precheck_reuses_ready_index() -> None:
    """Return the existing index without rebuilding it."""

    index_id = uuid4()

    existing = SimpleNamespace(
        id=index_id,
        status="ready",
        tree_sha="b" * 40,
    )

    session = make_session(existing)

    result = await lookup(session)

    assert result == index_id


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["building", "failed"])
async def test_precheck_rejects_unready_index(
    status: str,
) -> None:
    """Never reuse an incomplete or failed index."""

    existing = SimpleNamespace(
        id=uuid4(),
        status=status,
        tree_sha="b" * 40,
    )

    session = make_session(existing)

    with pytest.raises(
        RepositoryIndexConflict,
        match="not ready",
    ):
        await lookup(session)


@pytest.mark.anyio
async def test_precheck_rejects_tree_sha_mismatch() -> None:
    """Reject an index that references a different Git tree."""

    existing = SimpleNamespace(
        id=uuid4(),
        status="ready",
        tree_sha="c" * 40,
    )

    session = make_session(existing)

    with pytest.raises(
        RepositoryIndexConflict,
        match="different tree SHA",
    ):
        await lookup(session)


@pytest.mark.anyio
async def test_ready_index_skips_expensive_stages() -> None:
    """Reuse a ready index before claiming or building."""

    revision = make_revision()
    index_id = uuid4()
    provider = AsyncMock()

    with (
        patch(
            "maintainer_lens.services.repository_indexing.resolve_repository_revision",
            new_callable=AsyncMock,
            return_value=revision,
        ) as resolve_mock,
        patch(
            "maintainer_lens.services.repository_indexing._find_existing_index",
            new_callable=AsyncMock,
            return_value=index_id,
        ) as precheck_mock,
        patch(
            "maintainer_lens.services.repository_indexing."
            "try_acquire_repository_index_lease",
            new_callable=AsyncMock,
        ) as claim_mock,
        patch(
            "maintainer_lens.services.repository_indexing."
            "release_repository_index_lease",
            new_callable=AsyncMock,
        ) as release_mock,
        patch(
            "maintainer_lens.services.repository_indexing.fetch_repository_manifest",
            new_callable=AsyncMock,
        ) as manifest_mock,
        patch(
            "maintainer_lens.services.repository_indexing.download_repository_sources",
            new_callable=AsyncMock,
        ) as download_mock,
        patch(
            "maintainer_lens.services.repository_indexing.chunk_repository_source",
        ) as chunk_mock,
        patch(
            "maintainer_lens.services.repository_indexing.write_repository_index",
            new_callable=AsyncMock,
        ) as write_mock,
    ):
        result = await build_repository_index(
            github_http_client=AsyncMock(),
            embedding_provider_client=provider,
            session=AsyncMock(),
            repository=revision.repository,
            lease_session_factory=MagicMock(),
            embedding_provider_name="dashscope",
            embedding_model="test-model",
            embedding_dimensions=1024,
            chunker_version="v1",
        )

    assert result == index_id

    resolve_mock.assert_awaited_once()
    precheck_mock.assert_awaited_once()

    claim_mock.assert_not_awaited()
    release_mock.assert_not_awaited()

    manifest_mock.assert_not_awaited()
    download_mock.assert_not_awaited()
    chunk_mock.assert_not_called()

    provider.embed_documents.assert_not_awaited()
    write_mock.assert_not_awaited()
