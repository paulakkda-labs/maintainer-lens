"""Tests for repository index orchestration and embedding batches."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositoryManifest,
    RepositoryRevision,
    RepositorySource,
)
from maintainer_lens.services.repository_indexing import (
    build_repository_index,
    embed_repository_chunks,
)


def make_file(path: str = "src/sample.py") -> RepositoryFile:
    """Build a representative repository file."""

    return RepositoryFile(
        path=path,
        blob_sha="a" * 40,
        size=40,
        mode="100644",
    )


def make_chunk(symbol: str, content: str) -> CodeChunk:
    """Build a source chunk for batch tests."""

    return CodeChunk(
        file=make_file(),
        language="python",
        kind="function",
        symbol=symbol,
        symbols=[symbol],
        start_line=1,
        end_line=1,
        content=content,
    )


def make_revision() -> RepositoryRevision:
    """Build a fixed GitHub repository revision."""

    return RepositoryRevision(
        repository="pypa/sampleproject",
        default_branch="main",
        commit_sha="b" * 40,
        tree_sha="c" * 40,
    )


@pytest.mark.anyio
async def test_embedding_batches_preserve_order() -> None:
    """Embed all chunks in batches without changing source order."""

    chunks = [make_chunk(f"function_{index}", f"code_{index}") for index in range(45)]

    provider = AsyncMock()

    async def embed_batch(texts: list[str]) -> list[list[float]]:
        return [[float(int(text.split("_")[-1]))] for text in texts]

    provider.embed_documents.side_effect = embed_batch

    result = await embed_repository_chunks(
        provider,
        chunks,
        batch_size=20,
    )

    assert provider.embed_documents.await_count == 3
    assert [len(call.args[0]) for call in provider.embed_documents.await_args_list] == [
        20,
        20,
        5,
    ]

    assert [item.chunk for item in result] == chunks
    assert [item.vector for item in result] == [[float(index)] for index in range(45)]


@pytest.mark.anyio
async def test_embedding_batch_rejects_result_count_mismatch() -> None:
    """Reject a provider that returns too few vectors."""

    provider = AsyncMock()
    provider.embed_documents.return_value = [[0.1]]

    chunks = [
        make_chunk("first", "first()"),
        make_chunk("second", "second()"),
    ]

    with pytest.raises(ValueError, match="result count mismatch"):
        await embed_repository_chunks(
            provider,
            chunks,
            batch_size=2,
        )


@pytest.mark.anyio
async def test_embedding_batch_rejects_invalid_batch_size() -> None:
    """Validate batch size before invoking the provider."""

    with pytest.raises(ValueError, match="batch size"):
        await embed_repository_chunks(
            AsyncMock(),
            [make_chunk("first", "first()")],
            batch_size=0,
        )


@pytest.mark.anyio
async def test_embedding_batch_propagates_provider_failure() -> None:
    """Do not return partial embeddings after a provider failure."""

    provider = AsyncMock()
    provider.embed_documents.side_effect = RuntimeError("Embedding API unavailable")

    with pytest.raises(RuntimeError, match="Embedding API unavailable"):
        await embed_repository_chunks(
            provider,
            [make_chunk("first", "first()")],
        )


@pytest.mark.anyio
async def test_build_repository_index_connects_all_stages() -> None:
    """Resolve, claim, download, embed, persist, and release."""

    revision = make_revision()
    source_file = make_file()

    manifest = RepositoryManifest(
        repository=revision.repository,
        commit_sha=revision.commit_sha,
        tree_sha=revision.tree_sha,
        files=[source_file],
    )

    source = RepositorySource(
        file=source_file,
        content="def add_one(number):\n    return number + 1\n",
    )

    chunks = [make_chunk("add_one", source.content)]

    provider = AsyncMock()
    provider.embed_documents.return_value = [[0.1, 0.2]]

    index_id = uuid4()
    owner_token = uuid4()

    with (
        patch(
            "maintainer_lens.services.repository_indexing."
            "try_acquire_repository_index_lease",
            new_callable=AsyncMock,
            return_value=owner_token,
        ) as claim_mock,
        patch(
            "maintainer_lens.services.repository_indexing."
            "release_repository_index_lease",
            new_callable=AsyncMock,
            return_value=True,
        ) as release_mock,
        patch(
            "maintainer_lens.services.repository_indexing._find_existing_index",
            new_callable=AsyncMock,
            return_value=None,
        ) as precheck_mock,
        patch(
            "maintainer_lens.services.repository_indexing.resolve_repository_revision",
            new_callable=AsyncMock,
            return_value=revision,
        ) as resolve_mock,
        patch(
            "maintainer_lens.services.repository_indexing.fetch_repository_manifest",
            new_callable=AsyncMock,
            return_value=manifest,
        ) as manifest_mock,
        patch(
            "maintainer_lens.services.repository_indexing.download_repository_sources",
            new_callable=AsyncMock,
            return_value=[source],
        ) as download_mock,
        patch(
            "maintainer_lens.services.repository_indexing.chunk_repository_source",
            return_value=chunks,
        ) as chunk_mock,
        patch(
            "maintainer_lens.services.repository_indexing.write_repository_index",
            new_callable=AsyncMock,
            return_value=index_id,
        ) as write_mock,
    ):
        result = await build_repository_index(
            github_http_client=AsyncMock(),
            embedding_provider_client=provider,
            session=AsyncMock(),
            repository=revision.repository,
            lease_session_factory=MagicMock(),
            embedding_provider_name="dashscope",
            embedding_model="test-embedding-model",
            embedding_dimensions=2,
            chunker_version="v1",
            embedding_batch_size=20,
            download_concurrency=4,
        )

    assert result == index_id

    resolve_mock.assert_awaited_once()
    precheck_mock.assert_awaited()
    assert precheck_mock.await_count == 2

    claim_mock.assert_awaited_once()
    manifest_mock.assert_awaited_once()
    download_mock.assert_awaited_once()
    chunk_mock.assert_called_once_with(source)

    provider.embed_documents.assert_awaited_once_with([source.content])

    write_mock.assert_awaited_once()
    release_mock.assert_awaited_once()

    args = write_mock.await_args.args

    assert args[1] == revision
    assert len(args[2]) == 1
    assert args[2][0].vector == [0.1, 0.2]

    assert write_mock.await_args.kwargs["owner_token"] == owner_token

    assert release_mock.await_args.kwargs["owner_token"] == owner_token


@pytest.mark.anyio
async def test_build_repository_index_does_not_write_after_embedding_failure() -> None:
    """Never persist an index if embedding generation fails."""

    revision = make_revision()

    manifest = RepositoryManifest(
        repository=revision.repository,
        commit_sha=revision.commit_sha,
        tree_sha=revision.tree_sha,
        files=[make_file()],
    )

    source = RepositorySource(
        file=make_file(),
        content="def add_one(number):\n    return number + 1\n",
    )

    chunks = [make_chunk("add_one", source.content)]

    provider = AsyncMock()
    provider.embed_documents.side_effect = RuntimeError("Embedding API failed")

    owner_token = uuid4()

    with (
        patch(
            "maintainer_lens.services.repository_indexing."
            "try_acquire_repository_index_lease",
            new_callable=AsyncMock,
            return_value=owner_token,
        ) as claim_mock,
        patch(
            "maintainer_lens.services.repository_indexing."
            "release_repository_index_lease",
            new_callable=AsyncMock,
            return_value=True,
        ) as release_mock,
        patch(
            "maintainer_lens.services.repository_indexing._find_existing_index",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "maintainer_lens.services.repository_indexing.resolve_repository_revision",
            new_callable=AsyncMock,
            return_value=revision,
        ),
        patch(
            "maintainer_lens.services.repository_indexing.fetch_repository_manifest",
            new_callable=AsyncMock,
            return_value=manifest,
        ),
        patch(
            "maintainer_lens.services.repository_indexing.download_repository_sources",
            new_callable=AsyncMock,
            return_value=[source],
        ),
        patch(
            "maintainer_lens.services.repository_indexing.chunk_repository_source",
            return_value=chunks,
        ),
        patch(
            "maintainer_lens.services.repository_indexing.write_repository_index",
            new_callable=AsyncMock,
        ) as write_mock,
    ):
        with pytest.raises(
            RuntimeError,
            match="Embedding API failed",
        ):
            await build_repository_index(
                github_http_client=AsyncMock(),
                embedding_provider_client=provider,
                session=AsyncMock(),
                repository=revision.repository,
                lease_session_factory=MagicMock(),
                embedding_provider_name="dashscope",
                embedding_model="test-model",
                embedding_dimensions=2,
                chunker_version="v1",
            )

    claim_mock.assert_awaited_once()
    provider.embed_documents.assert_awaited_once()

    write_mock.assert_not_awaited()

    release_mock.assert_awaited_once()
    assert release_mock.await_args.kwargs["owner_token"] == owner_token
