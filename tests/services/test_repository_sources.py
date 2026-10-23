"""Tests for bounded repository source downloads."""

import asyncio
import base64
import hashlib

import httpx
import pytest

from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositoryManifest,
)
from maintainer_lens.services.repository_sources import (
    MAX_IN_MEMORY_SOURCE_BYTES,
    download_repository_sources,
)

REPOSITORY = "pypa/sampleproject"
COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40


def make_manifest(contents: dict[str, bytes]) -> RepositoryManifest:
    """Build a manifest from known source bytes."""

    files = []

    for path, content in contents.items():
        header = f"blob {len(content)}\0".encode("ascii")
        sha = hashlib.sha1(
            header + content,
            usedforsecurity=False,
        ).hexdigest()

        files.append(
            RepositoryFile(
                path=path,
                blob_sha=sha,
                size=len(content),
                mode="100644",
            )
        )

    return RepositoryManifest(
        repository=REPOSITORY,
        commit_sha=COMMIT_SHA,
        tree_sha=TREE_SHA,
        files=files,
    )


def make_blob_response(sha: str, content: bytes) -> httpx.Response:
    """Return a simulated GitHub Base64 blob response."""

    return httpx.Response(
        200,
        json={
            "sha": sha,
            "size": len(content),
            "encoding": "base64",
            "content": base64.b64encode(content).decode("ascii"),
        },
    )


@pytest.mark.anyio
async def test_download_sources_limits_concurrency_and_keeps_order() -> None:
    """Limit concurrent requests and preserve manifest ordering."""

    contents = {
        "src/a.py": b"a = 1\n",
        "src/b.py": b"b = 2\n",
        "src/c.py": b"c = 3\n",
    }
    manifest = make_manifest(contents)
    blobs = {file.blob_sha: contents[file.path] for file in manifest.files}

    active = 0
    peak = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak

        assert request.url.path.startswith(f"/repos/{REPOSITORY}/git/blobs/")

        sha = request.url.path.rsplit("/", 1)[-1]

        active += 1
        peak = max(peak, active)

        try:
            await asyncio.sleep(0.01)
            return make_blob_response(sha, blobs[sha])
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sources = await download_repository_sources(
            client,
            manifest,
            concurrency=2,
        )

    assert peak == 2
    assert [source.file.path for source in sources] == list(contents)
    assert [source.content for source in sources] == [
        content.decode("utf-8") for content in contents.values()
    ]


@pytest.mark.anyio
async def test_download_sources_rejects_corrupted_blob() -> None:
    """Do not return a partial batch when a blob is corrupted."""

    manifest = make_manifest({"src/a.py": b"good\n"})
    file = manifest.files[0]

    def respond(request: httpx.Request) -> httpx.Response:
        return make_blob_response(file.blob_sha, b"evil\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ExceptionGroup) as exc_info:
            await download_repository_sources(client, manifest)

    assert any(
        isinstance(error, ValueError) and "SHA mismatch" in str(error)
        for error in exc_info.value.exceptions
    )


@pytest.mark.anyio
async def test_download_sources_handles_empty_manifest() -> None:
    """An empty manifest requires no HTTP requests."""

    manifest = make_manifest({})

    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Unexpected HTTP request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sources = await download_repository_sources(client, manifest)

        assert sources == []

        with pytest.raises(ValueError, match="Invalid.*concurrency"):
            await download_repository_sources(
                client,
                manifest,
                concurrency=0,
            )


@pytest.mark.anyio
async def test_download_sources_rejects_oversized_batch() -> None:
    """Reject a batch that exceeds its in-memory byte budget."""

    manifest = RepositoryManifest(
        repository=REPOSITORY,
        commit_sha=COMMIT_SHA,
        tree_sha=TREE_SHA,
        files=[
            RepositoryFile(
                path="large.py",
                blob_sha="c" * 40,
                size=MAX_IN_MEMORY_SOURCE_BYTES + 1,
                mode="100644",
            )
        ],
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="memory budget"):
            await download_repository_sources(client, manifest)
