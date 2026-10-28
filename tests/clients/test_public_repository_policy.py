"""Enforce V1 public-repository boundaries before content retrieval."""

import httpx
import pytest

from maintainer_lens.clients.github import fetch_github_issue
from maintainer_lens.clients.github_history import fetch_repository_history_page
from maintainer_lens.clients.github_history_details import (
    fetch_issue_comments_page,
    fetch_pull_request_details,
)
from maintainer_lens.clients.github_repository import resolve_repository_revision

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize(
    "operation",
    [
        "issue",
        "history",
        "comments",
        "pull_request",
        "source_revision",
    ],
)
@pytest.mark.parametrize(
    "metadata",
    [
        {"private": True},
        {},
        {"private": "false"},
        {"private": 0},
        [],
    ],
)
async def test_private_or_unknown_visibility_never_reaches_content(operation, metadata):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        assert request.url.path == "/repos/org/repo"
        return httpx.Response(200, json=metadata)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"Authorization": "Bearer fake-private-capable-token"},
    ) as client:
        with pytest.raises(ValueError, match="verified public"):
            if operation == "issue":
                await fetch_github_issue(client, "org/repo", 1)
            elif operation == "history":
                await fetch_repository_history_page(client, "org/repo")
            elif operation == "comments":
                await fetch_issue_comments_page(client, "org/repo", 1)
            elif operation == "pull_request":
                await fetch_pull_request_details(client, "org/repo", 1)
            else:
                await resolve_repository_revision(client, "org/repo")
    assert requests == ["/repos/org/repo"]


@pytest.mark.parametrize("change", ["number", "url", "case_only"])
async def test_issue_response_identity_matches_the_requested_issue(change):
    payload = {
        "number": 2 if change == "number" else 1,
        "title": "Cleanup issue",
        "body": None,
        "state": "open",
        "html_url": (
            "https://github.com/other/repo/issues/1"
            if change == "url"
            else "https://github.com/org/repo/issues/1"
        ),
        "created_at": "2026-10-27T00:00:00Z",
        "updated_at": "2026-10-28T00:00:00Z",
    }

    def respond(request):
        return httpx.Response(
            200,
            json={"private": False} if request.url.path.count("/") == 3 else payload,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if change == "case_only":
            assert (await fetch_github_issue(client, "Org/Repo", 1)).number == 1
        else:
            with pytest.raises(ValueError, match="unexpected"):
                await fetch_github_issue(client, "org/repo", 1)
