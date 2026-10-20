"""GitHub REST API client for public issue retrieval."""

import re
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, Field, HttpUrl

GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"


class GitHubIssue(BaseModel):
    """Relevant fields from a GitHub issue response."""

    number: int = Field(gt=0)
    title: str
    body: str | None
    state: Literal["open", "closed"]
    html_url: HttpUrl
    created_at: datetime
    updated_at: datetime


async def fetch_github_issue(
    http_client: httpx.AsyncClient,
    repository: str,
    issue_number: int,
) -> GitHubIssue:
    """Fetch a public GitHub issue and validate its response."""

    parts = repository.split("/")

    if len(parts) != 2 or any(
        part in (".", "..") or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
        for part in parts
    ):
        raise ValueError("Invalid GitHub repository")

    if type(issue_number) is not int or issue_number <= 0:
        raise ValueError("Invalid GitHub issue number")

    owner, repo = parts

    response = await http_client.get(
        f"{GITHUB_API_BASE_URL}/repos/{owner}/{repo}/issues/{issue_number}",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )

    response.raise_for_status()

    payload = response.json()

    if "pull_request" in payload:
        raise ValueError("Pull request is not a GitHub issue")

    return GitHubIssue.model_validate(payload)
