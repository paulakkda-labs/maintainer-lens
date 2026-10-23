"""Tests for GitHub Git blob retrieval and content verification."""

import base64
import hashlib

import httpx
import pytest

from maintainer_lens.clients.github_blob import (
    _calculate_blob_sha,
    fetch_repository_blob,
)
from maintainer_lens.schemas.repository import RepositoryFile

REPOSITORY = "pypa/sampleproject"
CONTENT = b'print("hello")\n'


def make_file(content: bytes) -> RepositoryFile:
    """Create file metadata with the Git object ID for these bytes."""

    header = f"blob {len(content)}\0".encode("ascii")
    sha = hashlib.sha1(
        header + content,
        usedforsecurity=False,
    ).hexdigest()

    return RepositoryFile(
        path="src/sample/simple.py",
        blob_sha=sha,
        size=len(content),
        mode="100644",
    )


def make_transport(
    file: RepositoryFile,
    content: bytes,
    *,
    overrides: dict | None = None,
    status_code: int = 200,
) -> httpx.MockTransport:
    """Simulate a GitHub blob response."""

    encoded = base64.b64encode(content).decode("ascii")

    payload = {
        "sha": file.blob_sha,
        "size": len(content),
        "encoding": "base64",
        "content": encoded[:12] + "\n" + encoded[12:],
    }

    if overrides is not None:
        payload.update(overrides)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (
            f"/repos/pypa/sampleproject/git/blobs/{file.blob_sha}"
        )

        if status_code != 200:
            return httpx.Response(status_code)

        return httpx.Response(200, json=payload)

    return httpx.MockTransport(respond)


def test_git_blob_sha_matches_known_git_object() -> None:
    """Verify the Git object hashing formula against a known value."""

    assert _calculate_blob_sha(b"hello\n") == (
        "ce013625030ba8dba906f756967f9e9ca394464a"
    )


@pytest.mark.anyio
async def test_fetch_repository_blob_success() -> None:
    """Download Base64 content and verify its Git object identity."""

    file = make_file(CONTENT)

    async with httpx.AsyncClient(transport=make_transport(file, CONTENT)) as client:
        text = await fetch_repository_blob(client, REPOSITORY, file)

    assert text == 'print("hello")\n'


@pytest.mark.anyio
async def test_fetch_repository_blob_wrong_response_sha() -> None:
    """Reject a response claiming an unexpected blob SHA."""

    file = make_file(CONTENT)

    async with httpx.AsyncClient(
        transport=make_transport(
            file,
            CONTENT,
            overrides={"sha": "f" * 40},
        )
    ) as client:
        with pytest.raises(ValueError, match="Unexpected Git blob SHA"):
            await fetch_repository_blob(client, REPOSITORY, file)


@pytest.mark.anyio
async def test_fetch_repository_blob_corrupted_content() -> None:
    """Detect altered bytes even if the response claims the expected SHA."""

    file = make_file(CONTENT)
    altered_content = CONTENT.replace(b"hello", b"world")

    async with httpx.AsyncClient(
        transport=make_transport(file, altered_content)
    ) as client:
        with pytest.raises(
            ValueError,
            match="Git blob content SHA mismatch",
        ):
            await fetch_repository_blob(client, REPOSITORY, file)


@pytest.mark.anyio
async def test_fetch_repository_blob_wrong_size() -> None:
    """Reject a blob whose declared size differs from the manifest."""

    file = make_file(CONTENT)

    async with httpx.AsyncClient(
        transport=make_transport(
            file,
            CONTENT,
            overrides={"size": file.size + 1},
        )
    ) as client:
        with pytest.raises(ValueError, match="Unexpected Git blob size"):
            await fetch_repository_blob(client, REPOSITORY, file)


@pytest.mark.anyio
async def test_fetch_repository_blob_invalid_base64() -> None:
    """Reject malformed Base64 data."""

    file = make_file(CONTENT)

    async with httpx.AsyncClient(
        transport=make_transport(
            file,
            CONTENT,
            overrides={"content": "not-valid-***"},
        )
    ) as client:
        with pytest.raises(ValueError, match="Invalid Base64"):
            await fetch_repository_blob(client, REPOSITORY, file)


@pytest.mark.anyio
async def test_fetch_repository_blob_invalid_utf8() -> None:
    """Reject a file that cannot be decoded as UTF-8 text."""

    binary_content = b"\xff\xfe\xff"
    file = make_file(binary_content)

    async with httpx.AsyncClient(
        transport=make_transport(file, binary_content)
    ) as client:
        with pytest.raises(ValueError, match="not UTF-8"):
            await fetch_repository_blob(client, REPOSITORY, file)


@pytest.mark.anyio
async def test_fetch_repository_blob_http_error() -> None:
    """Preserve HTTP errors for the upper-level retry policy."""

    file = make_file(CONTENT)

    async with httpx.AsyncClient(
        transport=make_transport(file, CONTENT, status_code=404)
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_repository_blob(client, REPOSITORY, file)
