"""Tests for leased repository index orchestration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest

from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositoryRevision,
)
from maintainer_lens.services import repository_indexing as indexing
from maintainer_lens.services.repository_index_lease import (
    RepositoryIndexLeaseLost,
)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Mock external dependencies while preserving orchestration logic."""

    revision = RepositoryRevision(
        repository="pypa/sampleproject",
        default_branch="main",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
    )

    content = "def add_one(value):\n    return value + 1\n"

    chunk = CodeChunk(
        file=RepositoryFile(
            path="src/sample.py",
            blob_sha="c" * 40,
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

    token = uuid4()
    index_id = uuid4()

    resolve = AsyncMock(return_value=revision)
    precheck = AsyncMock(return_value=None)
    acquire = AsyncMock(return_value=token)
    renew = AsyncMock(return_value=True)
    release = AsyncMock(return_value=True)

    manifest = AsyncMock(return_value=object())
    download = AsyncMock(return_value=[object()])
    chunker = Mock(return_value=[chunk])
    writer = AsyncMock(return_value=index_id)

    provider = AsyncMock()
    provider.embed_documents.return_value = [[0.1, 0.2]]

    # Each factory call provides a context-managed lease session.
    lease_session = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=lease_session)
    context.__aexit__ = AsyncMock(return_value=False)

    lease_factory = MagicMock(return_value=context)

    dependencies = {
        "resolve_repository_revision": resolve,
        "_find_existing_index": precheck,
        "try_acquire_repository_index_lease": acquire,
        "renew_repository_index_lease": renew,
        "release_repository_index_lease": release,
        "fetch_repository_manifest": manifest,
        "download_repository_sources": download,
        "chunk_repository_source": chunker,
        "write_repository_index": writer,
    }

    for name, replacement in dependencies.items():
        monkeypatch.setattr(indexing, name, replacement)

    return SimpleNamespace(
        revision=revision,
        token=token,
        index_id=index_id,
        resolve=resolve,
        precheck=precheck,
        acquire=acquire,
        renew=renew,
        release=release,
        manifest=manifest,
        download=download,
        chunker=chunker,
        writer=writer,
        provider=provider,
        lease_factory=lease_factory,
    )


async def run_build(
    harness: SimpleNamespace,
    *,
    lease_seconds: int = 300,
    renewal_interval_seconds: float | None = None,
):
    """Execute the orchestration with mocked external services."""

    return await indexing.build_repository_index(
        github_http_client=AsyncMock(),
        embedding_provider_client=harness.provider,
        session=AsyncMock(),
        repository=harness.revision.repository,
        lease_session_factory=harness.lease_factory,
        embedding_provider_name="test-provider",
        embedding_model="test-model",
        embedding_dimensions=2,
        chunker_version="test-v1",
        lease_seconds=lease_seconds,
        renewal_interval_seconds=renewal_interval_seconds,
    )


@pytest.mark.anyio
async def test_ready_index_skips_claim_and_embedding(
    harness: SimpleNamespace,
) -> None:
    """Reuse an existing ready index without acquiring a lease."""

    harness.precheck.return_value = harness.index_id

    result = await run_build(harness)

    assert result == harness.index_id
    harness.acquire.assert_not_awaited()
    harness.manifest.assert_not_awaited()
    harness.provider.embed_documents.assert_not_awaited()
    harness.writer.assert_not_awaited()


@pytest.mark.anyio
async def test_failed_claim_does_not_start_build(
    harness: SimpleNamespace,
) -> None:
    """Do not build when another worker owns the lease."""

    harness.acquire.return_value = None

    with pytest.raises(indexing.RepositoryIndexBuildInProgress):
        await run_build(harness)

    assert harness.precheck.await_count == 2
    harness.manifest.assert_not_awaited()
    harness.provider.embed_documents.assert_not_awaited()
    harness.writer.assert_not_awaited()
    harness.release.assert_not_awaited()


@pytest.mark.anyio
async def test_second_precheck_reuses_completed_index(
    harness: SimpleNamespace,
) -> None:
    """Avoid rebuilding an index completed during the claim race."""

    harness.precheck.side_effect = [None, harness.index_id]

    result = await run_build(harness)

    assert result == harness.index_id
    harness.acquire.assert_awaited_once()
    harness.manifest.assert_not_awaited()
    harness.provider.embed_documents.assert_not_awaited()
    harness.writer.assert_not_awaited()
    harness.release.assert_awaited_once()


@pytest.mark.anyio
async def test_successful_build_passes_token_to_writer(
    harness: SimpleNamespace,
) -> None:
    """Build the index under the acquired lease and release afterward."""

    result = await run_build(harness)

    assert result == harness.index_id
    assert harness.precheck.await_count == 2

    harness.acquire.assert_awaited_once()
    harness.manifest.assert_awaited_once()
    harness.provider.embed_documents.assert_awaited_once()
    harness.writer.assert_awaited_once()
    harness.release.assert_awaited_once()

    assert harness.writer.await_args.kwargs["owner_token"] == harness.token


@pytest.mark.anyio
async def test_heartbeat_renews_during_embedding(
    harness: SimpleNamespace,
) -> None:
    """Keep renewing the lease while an embedding request is pending."""

    embedding_started = asyncio.Event()
    finish_embedding = asyncio.Event()

    async def delayed_embedding(
        texts: list[str],
    ) -> list[list[float]]:
        embedding_started.set()
        await finish_embedding.wait()
        return [[0.1, 0.2] for _ in texts]

    harness.provider.embed_documents.side_effect = delayed_embedding

    task = asyncio.create_task(
        run_build(
            harness,
            lease_seconds=3,
            renewal_interval_seconds=0.01,
        )
    )

    try:
        await asyncio.wait_for(embedding_started.wait(), timeout=2)

        for _ in range(100):
            if harness.renew.await_count >= 1:
                break
            await asyncio.sleep(0.01)

        assert harness.renew.await_count >= 1

    finally:
        finish_embedding.set()

    result = await asyncio.wait_for(task, timeout=2)

    assert result == harness.index_id
    harness.writer.assert_awaited_once()
    harness.release.assert_awaited_once()


@pytest.mark.anyio
async def test_renewal_failure_cancels_build(
    harness: SimpleNamespace,
) -> None:
    """Stop expensive work when the current worker loses its lease."""

    embedding_started = asyncio.Event()
    harness.renew.return_value = False

    async def blocked_embedding(
        texts: list[str],
    ) -> list[list[float]]:
        embedding_started.set()
        await asyncio.Event().wait()
        return [[0.1, 0.2] for _ in texts]

    harness.provider.embed_documents.side_effect = blocked_embedding

    task = asyncio.create_task(
        run_build(
            harness,
            lease_seconds=3,
            renewal_interval_seconds=0.01,
        )
    )

    await asyncio.wait_for(embedding_started.wait(), timeout=2)

    with pytest.raises(RepositoryIndexLeaseLost):
        await asyncio.wait_for(task, timeout=2)

    harness.writer.assert_not_awaited()
    harness.release.assert_awaited_once()


@pytest.mark.anyio
async def test_embedding_failure_releases_lease(
    harness: SimpleNamespace,
) -> None:
    """Release ownership when embedding generation fails."""

    harness.provider.embed_documents.side_effect = RuntimeError(
        "Embedding provider unavailable"
    )

    with pytest.raises(RuntimeError, match="Embedding provider unavailable"):
        await run_build(harness)

    harness.writer.assert_not_awaited()
    harness.release.assert_awaited_once()
