"""Tests for GitHub repository manifest retrieval."""

import httpx
import pytest

from maintainer_lens.clients import github_tree
from maintainer_lens.clients.github_tree import (
    RepositoryTreeTruncatedError,
    fetch_repository_manifest,
)
from maintainer_lens.schemas.repository import RepositoryRevision

COMMIT_SHA = "a" * 40
TREE_SHA = "b" * 40
BLOB_SHA = "c" * 40

REVISION = RepositoryRevision(
    repository="encode/httpx",
    default_branch="master",
    commit_sha=COMMIT_SHA,
    tree_sha=TREE_SHA,
)


def make_transport(
    payload: dict,
    status_code: int = 200,
) -> httpx.MockTransport:
    """Return a mock transport for the fixed Git tree endpoint."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == (f"/repos/encode/httpx/git/trees/{TREE_SHA}")
        assert request.url.params["recursive"] == "1"
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(respond)


def tree_payload(
    entries: list[dict],
    *,
    truncated: bool = False,
    sha: str = TREE_SHA,
) -> dict:
    """Build a GitHub tree response."""

    return {
        "sha": sha,
        "truncated": truncated,
        "tree": entries,
    }


@pytest.mark.anyio
async def test_fetch_repository_manifest_filters_files() -> None:
    """Include regular source files and exclude unsupported entries."""

    entries = [
        {
            "path": "src/app.py",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 100,
        },
        {
            "path": "README.md",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 200,
        },
        {
            "path": "src/start.py",
            "mode": "100755",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 300,
        },
        {
            "path": "src/link.py",
            "mode": "120000",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 12,
        },
        {
            "path": "vendor/module.py",
            "mode": "160000",
            "type": "commit",
            "sha": COMMIT_SHA,
        },
        {
            "path": "build/generated.py",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 100,
        },
        {
            "path": "assets/logo.png",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 500,
        },
        {
            "path": "src",
            "mode": "040000",
            "type": "tree",
            "sha": TREE_SHA,
        },
    ]

    async with httpx.AsyncClient(
        transport=make_transport(tree_payload(entries))
    ) as client:
        manifest = await fetch_repository_manifest(client, REVISION)

    assert manifest.repository == "encode/httpx"
    assert manifest.commit_sha == COMMIT_SHA
    assert manifest.tree_sha == TREE_SHA
    assert [file.path for file in manifest.files] == [
        "README.md",
        "src/app.py",
        "src/start.py",
    ]


@pytest.mark.anyio
async def test_fetch_repository_manifest_empty() -> None:
    """Allow a complete tree with no indexable files."""

    async with httpx.AsyncClient(transport=make_transport(tree_payload([]))) as client:
        manifest = await fetch_repository_manifest(client, REVISION)

    assert manifest.files == []


@pytest.mark.anyio
async def test_fetch_repository_manifest_truncated() -> None:
    """Reject an incomplete tree even after non-recursive fallback."""

    requests: list[bool] = []

    def respond(request: httpx.Request) -> httpx.Response:
        recursive = "recursive" in request.url.params
        requests.append(recursive)

        return httpx.Response(
            200,
            json=tree_payload([], truncated=True),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RepositoryTreeTruncatedError):
            await fetch_repository_manifest(client, REVISION)

    assert requests == [True, False]


@pytest.mark.anyio
async def test_fetch_repository_manifest_wrong_tree() -> None:
    """Reject a response for a different Git tree."""

    async with httpx.AsyncClient(
        transport=make_transport(tree_payload([], sha="d" * 40))
    ) as client:
        with pytest.raises(ValueError, match="unexpected tree SHA"):
            await fetch_repository_manifest(client, REVISION)


@pytest.mark.anyio
async def test_fetch_repository_manifest_missing_size() -> None:
    """Reject an eligible source entry with missing metadata."""

    entries = [
        {
            "path": "src/main.py",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
        }
    ]

    async with httpx.AsyncClient(
        transport=make_transport(tree_payload(entries))
    ) as client:
        with pytest.raises(ValueError, match="Missing file size"):
            await fetch_repository_manifest(client, REVISION)


@pytest.mark.anyio
async def test_fetch_repository_manifest_http_error() -> None:
    """Propagate GitHub HTTP failures."""

    async with httpx.AsyncClient(
        transport=make_transport({}, status_code=404)
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_repository_manifest(client, REVISION)


@pytest.mark.anyio
async def test_fetch_repository_manifest_recovers_truncated_tree() -> None:
    """Recover the full manifest through non-recursive subtree requests."""

    src_sha = "d" * 40
    core_sha = "e" * 40
    build_sha = "f" * 40

    requests: list[tuple[str, bool]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sha = request.url.path.rsplit("/", 1)[-1]
        recursive = "recursive" in request.url.params
        requests.append((sha, recursive))

        if recursive:
            assert sha == TREE_SHA
            return httpx.Response(
                200,
                json=tree_payload(
                    [
                        {
                            "path": "partial.py",
                            "mode": "100644",
                            "type": "blob",
                            "sha": BLOB_SHA,
                            "size": 100,
                        }
                    ],
                    truncated=True,
                ),
            )

        if sha == TREE_SHA:
            entries = [
                {
                    "path": "README.md",
                    "mode": "100644",
                    "type": "blob",
                    "sha": BLOB_SHA,
                    "size": 100,
                },
                {
                    "path": "src",
                    "mode": "040000",
                    "type": "tree",
                    "sha": src_sha,
                },
                {
                    "path": "build",
                    "mode": "040000",
                    "type": "tree",
                    "sha": build_sha,
                },
            ]
        elif sha == src_sha:
            entries = [
                {
                    "path": "app.py",
                    "mode": "100644",
                    "type": "blob",
                    "sha": BLOB_SHA,
                    "size": 200,
                },
                {
                    "path": "core",
                    "mode": "040000",
                    "type": "tree",
                    "sha": core_sha,
                },
            ]
        elif sha == core_sha:
            entries = [
                {
                    "path": "engine.py",
                    "mode": "100644",
                    "type": "blob",
                    "sha": BLOB_SHA,
                    "size": 300,
                },
            ]
        else:
            pytest.fail(f"Unexpected tree SHA: {sha}")

        return httpx.Response(
            200,
            json=tree_payload(entries, sha=sha),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        manifest = await fetch_repository_manifest(client, REVISION)

    assert [file.path for file in manifest.files] == [
        "README.md",
        "src/app.py",
        "src/core/engine.py",
    ]
    assert requests == [
        (TREE_SHA, True),
        (TREE_SHA, False),
        (src_sha, False),
        (core_sha, False),
    ]


@pytest.mark.anyio
async def test_fetch_repository_manifest_rejects_wrong_subtree() -> None:
    """Reject a subtree response with an unexpected SHA."""

    src_sha = "d" * 40

    def respond(request: httpx.Request) -> httpx.Response:
        if "recursive" in request.url.params:
            return httpx.Response(
                200,
                json=tree_payload([], truncated=True),
            )

        sha = request.url.path.rsplit("/", 1)[-1]

        if sha == TREE_SHA:
            return httpx.Response(
                200,
                json=tree_payload(
                    [
                        {
                            "path": "src",
                            "mode": "040000",
                            "type": "tree",
                            "sha": src_sha,
                        }
                    ]
                ),
            )

        return httpx.Response(
            200,
            json=tree_payload([], sha="f" * 40),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="unexpected tree SHA"):
            await fetch_repository_manifest(client, REVISION)


@pytest.mark.anyio
async def test_fetch_repository_manifest_limits_subtree_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject traversal exceeding its configured request budget."""

    monkeypatch.setattr(github_tree, "MAX_SUBTREE_REQUESTS", 1)

    def respond(request: httpx.Request) -> httpx.Response:
        if "recursive" in request.url.params:
            return httpx.Response(
                200,
                json=tree_payload([], truncated=True),
            )

        return httpx.Response(
            200,
            json=tree_payload(
                [
                    {
                        "path": "src",
                        "mode": "040000",
                        "type": "tree",
                        "sha": "d" * 40,
                    }
                ]
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(github_tree.RepositoryTreeTraversalLimitError):
            await fetch_repository_manifest(client, REVISION)


@pytest.mark.anyio
@pytest.mark.parametrize("truncated", [False, True])
async def test_manifest_audit_is_consistent_across_tree_strategies(
    truncated: bool,
) -> None:
    """Produce the same selection audit with recursive or fallback traversal."""

    src_sha = "d" * 40
    assets_sha = "e" * 40
    excluded_sha = "f" * 40

    root_entries = [
        {
            "path": "src",
            "mode": "040000",
            "type": "tree",
            "sha": src_sha,
        },
        {
            "path": "assets",
            "mode": "040000",
            "type": "tree",
            "sha": assets_sha,
        },
        {
            "path": "node_modules",
            "mode": "040000",
            "type": "tree",
            "sha": excluded_sha,
        },
        {
            "path": "uv.lock",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 100,
        },
        {
            "path": "link.py",
            "mode": "120000",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 10,
        },
        {
            "path": "external",
            "mode": "160000",
            "type": "commit",
            "sha": BLOB_SHA,
        },
    ]

    src_entries = [
        {
            "path": "main.ts",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 100,
        },
        {
            "path": "big.py",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 512 * 1024 + 1,
        },
        {
            "path": ".env.production",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 20,
        },
    ]

    assets_entries = [
        {
            "path": "logo.png",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 50,
        },
    ]

    recursive_entries = [
        *root_entries,
        *[{**entry, "path": f"src/{entry['path']}"} for entry in src_entries],
        *[{**entry, "path": f"assets/{entry['path']}"} for entry in assets_entries],
        {
            "path": "node_modules/package/index.ts",
            "mode": "100644",
            "type": "blob",
            "sha": BLOB_SHA,
            "size": 100,
        },
    ]

    requests: list[tuple[str, bool]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sha = request.url.path.rsplit("/", 1)[-1]
        recursive = "recursive" in request.url.params
        requests.append((sha, recursive))

        if recursive:
            assert sha == TREE_SHA

            return httpx.Response(
                200,
                json=tree_payload(
                    recursive_entries,
                    truncated=truncated,
                ),
            )

        if sha == TREE_SHA:
            entries = root_entries
        elif sha == src_sha:
            entries = src_entries
        elif sha == assets_sha:
            entries = assets_entries
        else:
            pytest.fail(f"Unexpected subtree request: {sha}")

        return httpx.Response(
            200,
            json=tree_payload(entries, sha=sha),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        manifest = await fetch_repository_manifest(client, REVISION)

    assert [file.path for file in manifest.files] == [
        "src/main.ts",
    ]

    assert {item.path: item.reason for item in manifest.skipped_paths} == {
        "assets/logo.png": "unsupported_format",
        "external": "non_regular_file",
        "link.py": "non_regular_file",
        "node_modules": "excluded_directory",
        "src/.env.production": "excluded_file",
        "src/big.py": "file_too_large",
        "uv.lock": "excluded_file",
    }

    if truncated:
        assert requests == [
            (TREE_SHA, True),
            (TREE_SHA, False),
            (src_sha, False),
            (assets_sha, False),
        ]
    else:
        assert requests == [(TREE_SHA, True)]
