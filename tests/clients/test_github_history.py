"""Tests for paginated GitHub issue and PR history retrieval."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from maintainer_lens.clients.github import GITHUB_API_VERSION
from maintainer_lens.clients.github_history import fetch_repository_history_page


def _issue(number: int, *, is_pr: bool = False) -> dict:
    item = {
        "number": number,
        "title": f"Topic {number}",
        "body": None if is_pr else "Description",
        "state": "closed" if is_pr else "open",
        "html_url": (
            f"https://github.com/org/repo/{'pull' if is_pr else 'issues'}/{number}"
        ),
        "created_at": "2026-10-24T01:00:00Z",
        "updated_at": "2026-10-25T01:00:00Z",
    }

    if is_pr:
        item["pull_request"] = {}

    return item


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_fetches_mixed_issue_and_pr_with_next_page() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        seen.append(request)

        return httpx.Response(
            200,
            json=[_issue(5), _issue(6, is_pr=True)],
            headers={
                "Link": (
                    '<https://api.github.com/repos/org/repo/issues?page=2>; rel="next"'
                )
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_repository_history_page(
            client, "org/repo", page=1, per_page=2
        )

    assert result.next_page == 2
    assert len(result.items) == 2

    assert [(item.number, item.kind) for item in result.items] == [
        (5, "issue"),
        (6, "pull_request"),
    ]

    assert result.items[0].repository == "org/repo"
    assert result.items[1].body is None
    assert result.items[0].updated_at.year == 2026

    assert str(seen[0].url).startswith("https://api.github.com/repos/org/repo/issues?")
    assert seen[0].url.params["state"] == "all"
    assert seen[0].url.params["sort"] == "updated"
    assert seen[0].url.params["direction"] == "desc"
    assert seen[0].url.params["per_page"] == "2"
    assert seen[0].url.params["page"] == "1"
    assert seen[0].headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION


@pytest.mark.anyio
async def test_no_next_link_means_last_page() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(200, json=[_issue(1)])
            )
        )
    ) as client:
        result = await fetch_repository_history_page(client, "org/repo", page=3)

    assert result.next_page is None
    assert [item.number for item in result.items] == [1]


@pytest.mark.anyio
async def test_since_is_normalized_to_utc() -> None:
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        captured.append(request.url.params["since"])
        return httpx.Response(200, json=[])

    since = datetime(
        2026,
        10,
        25,
        8,
        30,
        tzinfo=timezone(timedelta(hours=8)),
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_repository_history_page(
            client,
            "org/repo",
            updated_since=since,
        )

    assert captured == ["2026-10-25T00:30:00Z"]
    assert result.items == ()
    assert result.next_page is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("repo", "page", "per_page", "since"),
    [
        ("org", 1, 100, None),
        ("../repo", 1, 100, None),
        ("org//repo", 1, 100, None),
        ("org/repo", 0, 100, None),
        ("org/repo", True, 100, None),
        ("org/repo", 1, 0, None),
        ("org/repo", 1, 101, None),
        ("org/repo", 1, True, None),
        ("org/repo", 1, 100, datetime(2026, 10, 25)),
    ],
)
async def test_rejects_invalid_input_without_request(
    repo, page, per_page, since
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        raise AssertionError("Network request should not be made")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await fetch_repository_history_page(
                client,
                repo,
                page=page,
                per_page=per_page,
                updated_since=since,
            )


@pytest.mark.anyio
async def test_http_errors_propagate() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(403, json={"message": "rate limit"})
            )
        )
    ) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await fetch_repository_history_page(client, "org/repo")

    assert error.value.response.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"message": "unexpected"},
        [None],
        [_issue(0)],
    ],
)
async def test_rejects_malformed_response(payload) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(200, json=payload)
            )
        )
    ) as client:
        with pytest.raises((ValueError, ValidationError)):
            await fetch_repository_history_page(client, "org/repo")
