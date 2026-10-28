"""Fetch GitHub issue comments and pull-request details."""

import re
from dataclasses import dataclass
from datetime import datetime

import httpx
from pydantic import BaseModel, Field, HttpUrl

from maintainer_lens.clients.github import (
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
    fetch_public_repository_metadata,
)


class GitHubHistoryAuthor(BaseModel):
    """Minimal GitHub account identity for a history comment."""

    login: str


class GitHubHistoryComment(BaseModel):
    """A discussion comment belonging to an issue or pull request."""

    repository: str
    issue_number: int = Field(gt=0)
    id: int = Field(gt=0)
    body: str | None
    html_url: HttpUrl
    created_at: datetime
    updated_at: datetime
    user: GitHubHistoryAuthor | None


@dataclass(frozen=True, slots=True)
class GitHubHistoryCommentsPage:
    """One comment page with an optional continuation page."""

    comments: tuple[GitHubHistoryComment, ...]
    next_page: int | None


class GitHubPullRequestGitRef(BaseModel):
    """A Git reference returned in pull-request details."""

    sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")


class GitHubPullRequestDetails(BaseModel):
    """Merge and source revision metadata for a pull request."""

    repository: str
    number: int = Field(gt=0)
    html_url: HttpUrl
    merged: bool
    merged_at: datetime | None
    merge_commit_sha: str | None
    head: GitHubPullRequestGitRef
    base: GitHubPullRequestGitRef
    changed_files: int = Field(ge=0)


def _parse_repository_number(
    repository: str,
    number: int,
) -> tuple[str, str]:
    """Validate repository identity and issue/PR number."""

    parts = repository.split("/")

    if len(parts) != 2 or any(
        part in (".", "..") or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
        for part in parts
    ):
        raise ValueError("Invalid GitHub repository")

    if type(number) is not int or number <= 0:
        raise ValueError("Issue or pull-request number must be positive")

    return parts[0], parts[1]


async def fetch_issue_comments_page(
    http_client: httpx.AsyncClient,
    repository: str,
    issue_number: int,
    *,
    page: int = 1,
    per_page: int = 100,
) -> GitHubHistoryCommentsPage:
    """Fetch one page of discussion comments for an issue or PR."""

    owner, repo = _parse_repository_number(repository, issue_number)

    if type(page) is not int or page <= 0:
        raise ValueError("page must be a positive integer")

    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100")

    await fetch_public_repository_metadata(http_client, repository)

    response = await http_client.get(
        (f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments"),
        params={"page": page, "per_page": per_page},
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, list):
        raise ValueError("Expected a list of GitHub issue comments")

    comments: list[GitHubHistoryComment] = []

    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("Invalid GitHub issue comment")

        comments.append(
            GitHubHistoryComment.model_validate(
                {
                    **raw,
                    "repository": repository,
                    "issue_number": issue_number,
                }
            )
        )

    return GitHubHistoryCommentsPage(
        comments=tuple(comments),
        next_page=page + 1 if "next" in response.links else None,
    )


async def fetch_pull_request_details(
    http_client: httpx.AsyncClient,
    repository: str,
    pull_number: int,
) -> GitHubPullRequestDetails:
    """Fetch merge status and revision metadata for one pull request."""

    owner, repo = _parse_repository_number(repository, pull_number)

    await fetch_public_repository_metadata(http_client, repository)

    response = await http_client.get(
        f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/pulls/{pull_number}",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("Expected a GitHub pull-request object")

    details = GitHubPullRequestDetails.model_validate(
        {
            **payload,
            "repository": repository,
        }
    )

    if details.number != pull_number:
        raise ValueError("Unexpected GitHub pull-request number")

    return details
