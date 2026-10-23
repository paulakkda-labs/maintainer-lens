"""Tests for repository metadata ingestion orchestration."""

import httpx
import pytest

from maintainer_lens.services.repository_ingestion import (
    build_repository_manifest,
)

COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
BLOB_SHA = "c" * 40


@pytest.mark.anyio
async def test_build_repository_manifest_success() -> None:
    """Resolve a fixed revision before fetching its Git tree."""

    requested_paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requested_paths.append(path)

        if path == "/repos/encode/httpx":
            return httpx.Response(
                200,
                json={"default_branch": "master"},
            )

        if path == "/repos/encode/httpx/branches/master":
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

        if path == f"/repos/encode/httpx/git/trees/{TREE_SHA}":
            assert request.url.params["recursive"] == "1"

            return httpx.Response(
                200,
                json={
                    "sha": TREE_SHA,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "README.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": BLOB_SHA,
                            "size": 200,
                        },
                        {
                            "path": "src/main.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": BLOB_SHA,
                            "size": 100,
                        },
                    ],
                },
            )

        pytest.fail(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        manifest = await build_repository_manifest(
            client,
            "encode/httpx",
        )

    assert manifest.repository == "encode/httpx"
    assert manifest.commit_sha == COMMIT_SHA
    assert manifest.tree_sha == TREE_SHA
    assert [file.path for file in manifest.files] == [
        "README.md",
        "src/main.py",
    ]

    assert requested_paths == [
        "/repos/encode/httpx",
        "/repos/encode/httpx/branches/master",
        f"/repos/encode/httpx/git/trees/{TREE_SHA}",
    ]


@pytest.mark.anyio
async def test_build_repository_manifest_propagates_tree_error() -> None:
    """Do not return a manifest when tree retrieval fails."""

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/repos/encode/httpx":
            return httpx.Response(
                200,
                json={"default_branch": "main"},
            )

        if path == "/repos/encode/httpx/branches/main":
            return httpx.Response(
                200,
                json={
                    "commit": {
                        "sha": COMMIT_SHA,
                        "commit": {
                            "tree": {"sha": TREE_SHA},
                        },
                    }
                },
            )

        if path == f"/repos/encode/httpx/git/trees/{TREE_SHA}":
            return httpx.Response(503)

        pytest.fail(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await build_repository_manifest(
                client,
                "encode/httpx",
            )

    assert exc_info.value.response.status_code == 503
