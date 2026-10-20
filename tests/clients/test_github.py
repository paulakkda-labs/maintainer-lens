"""Tests for GitHub issue retrieval."""

import httpx
import pytest

from maintainer_lens.clients.github import (
    GITHUB_API_VERSION,
    fetch_github_issue,
)

ISSUE_RESPONSE = {
    "number": 123,
    "title": "Example issue",
    "body": "Something is not working.",
    "state": "open",
    "html_url": "https://github.com/fastapi/fastapi/issues/123",
    "created_at": "2026-10-01T10:00:00Z",
    "updated_at": "2026-10-02T10:00:00Z",
}


@pytest.mark.anyio
async def test_fetch_github_issue_success() -> None:
    """Fetch and validate a public GitHub issue."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://api.github.com/repos/fastapi/fastapi/issues/123"
        )
        assert request.headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION

        return httpx.Response(200, json=ISSUE_RESPONSE)

    transport = httpx.MockTransport(respond)

    async with httpx.AsyncClient(transport=transport) as client:
        issue = await fetch_github_issue(
            http_client=client,
            repository="fastapi/fastapi",
            issue_number=123,
        )

    assert issue.number == 123
    assert issue.title == "Example issue"
    assert issue.body == "Something is not working."
    assert issue.state == "open"


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [404, 500])
async def test_fetch_github_issue_http_failure(
    status_code: int,
) -> None:
    """Propagate GitHub HTTP errors to the caller."""

    transport = httpx.MockTransport(lambda request: httpx.Response(status_code))

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await fetch_github_issue(
                http_client=client,
                repository="fastapi/fastapi",
                issue_number=123,
            )

    assert exc_info.value.response.status_code == status_code


@pytest.mark.anyio
async def test_fetch_github_issue_rejects_pull_request() -> None:
    """Reject pull requests returned by the Issues API."""

    payload = {
        **ISSUE_RESPONSE,
        "pull_request": {
            "url": "https://api.github.com/example",
        },
    }

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="Pull request"):
            await fetch_github_issue(
                http_client=client,
                repository="fastapi/fastapi",
                issue_number=123,
            )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "repository",
    [
        "",
        "fastapi",
        "fastapi/fastapi/extra",
        "../fastapi",
        "fastapi/repo?x=1",
    ],
)
async def test_fetch_github_issue_rejects_invalid_repository(
    repository: str,
) -> None:
    """Reject invalid repository names before sending a request."""

    transport = httpx.MockTransport(
        lambda request: pytest.fail("Unexpected HTTP request")
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="Invalid GitHub repository"):
            await fetch_github_issue(
                http_client=client,
                repository=repository,
                issue_number=123,
            )
