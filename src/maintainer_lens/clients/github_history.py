"""Retrieve paginated GitHub issue and pull-request history."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx

from maintainer_lens.clients.github import (
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
    GitHubIssue,
)


class GitHubHistoryItem(GitHubIssue):
    """A repository issue or pull request with its common issue metadata."""

    repository: str
    kind: Literal["issue", "pull_request"]


@dataclass(frozen=True, slots=True)
class GitHubHistoryPage:
    """One page of history, with an optional continuation page number."""

    items: tuple[GitHubHistoryItem, ...]
    next_page: int | None


async def fetch_repository_history_page(
    http_client: httpx.AsyncClient,
    repository: str,
    *,
    page: int = 1,
    per_page: int = 100,
    updated_since: datetime | None = None,
    sort_by: Literal["created", "updated"] = "updated",
    direction: Literal["asc", "desc"] = "desc",
) -> GitHubHistoryPage:
    """Fetch issues and PRs ordered by their latest update time."""

    parts = repository.split("/")

    if len(parts) != 2 or any(
        part in (".", "..") or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
        for part in parts
    ):
        raise ValueError("Invalid GitHub repository")

    if type(page) is not int or page <= 0:
        raise ValueError("page must be a positive integer")

    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100")

    if sort_by not in ("created", "updated"):
        raise ValueError("Invalid history sort field")

    if direction not in ("asc", "desc"):
        raise ValueError("Invalid history sort direction")

    params: dict[str, str | int] = {
        "state": "all",
        "sort": sort_by,
        "direction": direction,
        "page": page,
        "per_page": per_page,
    }

    if updated_since is not None:
        if updated_since.tzinfo is None or updated_since.utcoffset() is None:
            raise ValueError("updated_since must be timezone-aware")

        params["since"] = (
            updated_since.astimezone(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    owner, repo = parts

    response = await http_client.get(
        f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/issues",
        params=params,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, list):
        raise ValueError("Expected a list of GitHub issues")

    items: list[GitHubHistoryItem] = []

    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("Invalid GitHub history item")

        items.append(
            GitHubHistoryItem.model_validate(
                {
                    **raw,
                    "repository": repository,
                    "kind": "pull_request" if "pull_request" in raw else "issue",
                }
            )
        )

    return GitHubHistoryPage(
        items=tuple(items),
        next_page=page + 1 if "next" in response.links else None,
    )
