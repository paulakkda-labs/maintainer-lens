"""GitHub REST API client for repository revision resolution."""

import re
from urllib.parse import quote

import httpx
from pydantic import AliasPath, BaseModel, Field

from maintainer_lens.clients.github import (
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
    fetch_public_repository_metadata,
)
from maintainer_lens.schemas.repository import RepositoryRevision


class _RepositoryResponse(BaseModel):
    """Relevant fields from GitHub repository metadata."""

    default_branch: str = Field(min_length=1)


class _BranchResponse(BaseModel):
    """Relevant commit and tree SHAs from a GitHub branch response."""

    commit_sha: str = Field(
        validation_alias=AliasPath("commit", "sha"),
    )
    tree_sha: str = Field(
        validation_alias=AliasPath("commit", "commit", "tree", "sha"),
    )


async def resolve_repository_revision(
    http_client: httpx.AsyncClient,
    repository: str,
) -> RepositoryRevision:
    """Resolve the default branch to a fixed commit and root tree."""

    parts = repository.split("/")

    if len(parts) != 2 or any(
        part in (".", "..") or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
        for part in parts
    ):
        raise ValueError("Invalid GitHub repository")

    owner, repo = parts

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }

    metadata = await fetch_public_repository_metadata(http_client, repository)
    repository_data = _RepositoryResponse.model_validate(metadata)

    branch = quote(repository_data.default_branch, safe="")

    branch_response = await http_client.get(
        f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/branches/{branch}",
        headers=headers,
    )
    branch_response.raise_for_status()

    branch_data = _BranchResponse.model_validate(branch_response.json())

    return RepositoryRevision(
        repository=repository,
        default_branch=repository_data.default_branch,
        commit_sha=branch_data.commit_sha,
        tree_sha=branch_data.tree_sha,
    )
