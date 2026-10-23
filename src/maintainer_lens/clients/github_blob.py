"""Retrieve and verify source files from GitHub Git blobs."""

import base64
import binascii
import hashlib
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from maintainer_lens.clients.github import (
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
)
from maintainer_lens.schemas.repository import RepositoryFile


class _GitBlobResponse(BaseModel):
    """Validated GitHub Git blob API response."""

    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    size: int = Field(ge=0)
    encoding: Literal["base64"]
    content: str


def _calculate_blob_sha(content: bytes) -> str:
    """Calculate the SHA-1 object ID of a Git blob."""

    header = f"blob {len(content)}\0".encode("ascii")

    return hashlib.sha1(
        header + content,
        usedforsecurity=False,
    ).hexdigest()


async def fetch_repository_blob(
    http_client: httpx.AsyncClient,
    repository: str,
    file: RepositoryFile,
) -> str:
    """Download and verify a UTF-8 source file by its Git blob SHA."""

    owner, repo = repository.split("/")

    response = await http_client.get(
        (f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/git/blobs/{file.blob_sha}"),
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    response.raise_for_status()

    blob = _GitBlobResponse.model_validate(response.json())

    if blob.sha != file.blob_sha:
        raise ValueError(f"Unexpected Git blob SHA: {file.path}")

    if blob.size != file.size:
        raise ValueError(f"Unexpected Git blob size: {file.path}")

    try:
        encoded = "".join(blob.content.split())
        raw_content = base64.b64decode(
            encoded,
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid Base64 Git blob content: {file.path}") from exc

    if len(raw_content) != file.size:
        raise ValueError(f"Decoded Git blob size mismatch: {file.path}")

    actual_sha = _calculate_blob_sha(raw_content)

    if actual_sha != file.blob_sha:
        raise ValueError(f"Git blob content SHA mismatch: {file.path}")

    try:
        return raw_content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Git blob is not UTF-8 text: {file.path}") from exc
