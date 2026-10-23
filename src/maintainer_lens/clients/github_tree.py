"""GitHub repository tree retrieval and manifest construction."""

from collections import deque
from pathlib import PurePosixPath
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from maintainer_lens.clients.github import (
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
)
from maintainer_lens.core.source_types import (
    detect_source_type,
    is_excluded_source,
)
from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositoryManifest,
    RepositoryRevision,
    RepositorySkippedPath,
)

MAX_SOURCE_FILE_BYTES = 512 * 1024
MAX_SUBTREE_REQUESTS = 250

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        ".tox",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
    }
)


class RepositoryTreeTruncatedError(RuntimeError):
    """A non-recursive Git tree response was incomplete."""


class RepositoryTreeTraversalLimitError(RuntimeError):
    """The subtree traversal exceeded its request budget."""


class _GitTreeEntry(BaseModel):
    """A single entry returned by the GitHub Trees API."""

    path: str = Field(min_length=1)
    mode: str
    type: Literal["blob", "tree", "commit"]
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    size: int | None = Field(default=None, ge=0)


class _GitTreeResponse(BaseModel):
    """Validated GitHub tree response."""

    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    truncated: bool
    tree: list[_GitTreeEntry]


def _excluded_directory(
    path: str,
    *,
    is_directory: bool,
) -> str | None:
    """Find the first excluded directory in a repository path."""

    parts = PurePosixPath(path).parts

    directories = parts if is_directory else parts[:-1]

    for index, part in enumerate(directories):
        if part in EXCLUDED_DIRECTORIES:
            return "/".join(parts[: index + 1])

    return None


def _record_skip(
    skipped: dict[str, RepositorySkippedPath],
    path: str,
    reason: Literal[
        "excluded_directory",
        "excluded_file",
        "unsupported_format",
        "file_too_large",
        "non_regular_file",
    ],
) -> None:
    """Record a skipped path once."""

    if path not in skipped:
        skipped[path] = RepositorySkippedPath(
            path=path,
            reason=reason,
        )


def _collect_entry(
    entry: _GitTreeEntry,
    full_path: str,
    files: list[RepositoryFile],
    skipped: dict[str, RepositorySkippedPath],
) -> bool:
    """Classify an entry and report whether its subtree needs traversal."""

    excluded = _excluded_directory(
        full_path,
        is_directory=entry.type == "tree",
    )

    if excluded is not None:
        _record_skip(
            skipped,
            excluded,
            "excluded_directory",
        )
        return False

    if entry.type == "tree":
        return True

    if entry.type != "blob" or entry.mode not in ("100644", "100755"):
        _record_skip(
            skipped,
            full_path,
            "non_regular_file",
        )
        return False

    if is_excluded_source(full_path):
        _record_skip(
            skipped,
            full_path,
            "excluded_file",
        )
        return False

    if detect_source_type(full_path) is None:
        _record_skip(
            skipped,
            full_path,
            "unsupported_format",
        )
        return False

    if entry.size is None:
        raise ValueError(f"Missing file size: {full_path}")

    if entry.size > MAX_SOURCE_FILE_BYTES:
        _record_skip(
            skipped,
            full_path,
            "file_too_large",
        )
        return False

    files.append(
        RepositoryFile(
            path=full_path,
            blob_sha=entry.sha,
            size=entry.size,
            mode=entry.mode,
        )
    )

    return False


async def _fetch_tree(
    http_client: httpx.AsyncClient,
    repository: str,
    tree_sha: str,
    *,
    recursive: bool,
) -> _GitTreeResponse:
    """Fetch and validate a Git tree by its immutable SHA."""

    owner, repo = repository.split("/")

    response = await http_client.get(
        f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/git/trees/{tree_sha}",
        params={"recursive": "1"} if recursive else None,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    response.raise_for_status()

    tree_data = _GitTreeResponse.model_validate(response.json())

    if tree_data.sha != tree_sha:
        raise ValueError("GitHub returned an unexpected tree SHA")

    return tree_data


async def _fetch_entries_by_subtrees(
    http_client: httpx.AsyncClient,
    revision: RepositoryRevision,
) -> tuple[list[RepositoryFile], list[RepositorySkippedPath]]:
    """Recover and classify entries through non-recursive tree requests."""

    pending = deque([(revision.tree_sha, "")])
    files: list[RepositoryFile] = []
    skipped: dict[str, RepositorySkippedPath] = {}
    request_count = 0

    while pending:
        if request_count >= MAX_SUBTREE_REQUESTS:
            raise RepositoryTreeTraversalLimitError(
                f"Git tree request limit exceeded: {revision.repository}"
            )

        tree_sha, prefix = pending.popleft()

        tree_data = await _fetch_tree(
            http_client,
            revision.repository,
            tree_sha,
            recursive=False,
        )
        request_count += 1

        if tree_data.truncated:
            raise RepositoryTreeTruncatedError(
                f"Non-recursive Git tree was truncated: {prefix or '/'}"
            )

        for entry in tree_data.tree:
            if entry.path in (".", "..") or "/" in entry.path:
                raise ValueError(f"Invalid non-recursive Git tree path: {entry.path}")

            full_path = f"{prefix}/{entry.path}" if prefix else entry.path

            should_descend = _collect_entry(
                entry,
                full_path,
                files,
                skipped,
            )

            if should_descend:
                pending.append((entry.sha, full_path))

    return files, list(skipped.values())


async def fetch_repository_manifest(
    http_client: httpx.AsyncClient,
    revision: RepositoryRevision,
) -> RepositoryManifest:
    """Build an auditable manifest for a fixed Git revision."""

    tree_data = await _fetch_tree(
        http_client,
        revision.repository,
        revision.tree_sha,
        recursive=True,
    )

    if tree_data.truncated:
        # Discard the incomplete recursive response.
        files, skipped_paths = await _fetch_entries_by_subtrees(
            http_client,
            revision,
        )
    else:
        files = []
        skipped: dict[str, RepositorySkippedPath] = {}

        for entry in tree_data.tree:
            _collect_entry(
                entry,
                entry.path,
                files,
                skipped,
            )

        skipped_paths = list(skipped.values())

    files.sort(key=lambda file: file.path)
    skipped_paths.sort(key=lambda item: item.path)

    return RepositoryManifest(
        repository=revision.repository,
        commit_sha=revision.commit_sha,
        tree_sha=revision.tree_sha,
        files=files,
        skipped_paths=skipped_paths,
    )
