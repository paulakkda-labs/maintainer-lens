"""Tests for GitHub repository revision resolution."""

import httpx
import pytest
from pydantic import ValidationError

from maintainer_lens.clients.github_repository import (
    resolve_repository_revision,
)

COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40


@pytest.mark.anyio
async def test_resolve_repository_revision_success() -> None:
    """Resolve a branch to a fixed commit and tree."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"

        if request.url.path == "/repos/encode/httpx":
            return httpx.Response(
                200,
                json={"default_branch": "master"},
            )

        if request.url.path == "/repos/encode/httpx/branches/master":
            return httpx.Response(
                200,
                json={
                    "commit": {
                        "sha": COMMIT_SHA,
                        "commit": {
                            "tree": {
                                "sha": TREE_SHA,
                            }
                        },
                    }
                },
            )

        pytest.fail(f"Unexpected HTTP request: {request.url}")

    transport = httpx.MockTransport(respond)

    async with httpx.AsyncClient(transport=transport) as client:
        revision = await resolve_repository_revision(
            http_client=client,
            repository="encode/httpx",
        )

    assert revision.repository == "encode/httpx"
    assert revision.default_branch == "master"
    assert revision.commit_sha == COMMIT_SHA
    assert revision.tree_sha == TREE_SHA


@pytest.mark.anyio
async def test_resolve_repository_revision_not_found() -> None:
    """Propagate repository-not-found HTTP errors."""

    transport = httpx.MockTransport(lambda request: httpx.Response(404))

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await resolve_repository_revision(
                http_client=client,
                repository="missing/repository",
            )

    assert exc_info.value.response.status_code == 404


@pytest.mark.anyio
async def test_resolve_repository_revision_missing_tree_sha() -> None:
    """Reject a branch response without the root tree SHA."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/encode/httpx":
            return httpx.Response(
                200,
                json={"default_branch": "main"},
            )

        return httpx.Response(
            200,
            json={
                "commit": {
                    "sha": COMMIT_SHA,
                    "commit": {
                        "tree": {},
                    },
                }
            },
        )

    transport = httpx.MockTransport(respond)

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValidationError):
            await resolve_repository_revision(
                http_client=client,
                repository="encode/httpx",
            )


@pytest.mark.anyio
async def test_resolve_repository_revision_invalid_repository() -> None:
    """Reject invalid repository names before making HTTP requests."""

    transport = httpx.MockTransport(
        lambda request: pytest.fail("Unexpected HTTP request")
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="Invalid GitHub repository"):
            await resolve_repository_revision(
                http_client=client,
                repository="../httpx",
            )
