"""Tests for GitHub history comments and pull-request details."""

import httpx
import pytest
from pydantic import ValidationError

from maintainer_lens.clients.github import GITHUB_API_VERSION
from maintainer_lens.clients.github_history_details import (
    fetch_issue_comments_page,
    fetch_pull_request_details,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _comment(
    comment_id: int,
    *,
    user: dict | None = None,
) -> dict:
    return {
        "id": comment_id,
        "body": f"Discussion {comment_id}",
        "html_url": (
            f"https://github.com/org/repo/issues/10#issuecomment-{comment_id}"
        ),
        "created_at": "2026-10-20T10:00:00Z",
        "updated_at": "2026-10-21T10:00:00Z",
        "user": user,
    }


def _pull_request(number: int, *, merged: bool = True) -> dict:
    return {
        "number": number,
        "html_url": f"https://github.com/org/repo/pull/{number}",
        "merged": merged,
        "merged_at": "2026-10-22T10:00:00Z" if merged else None,
        "merge_commit_sha": "c" * 40,
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40},
        "changed_files": 5,
    }


async def test_fetch_issue_comments_page() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        requests.append(request)

        return httpx.Response(
            200,
            json=[
                _comment(100, user={"login": "maintainer"}),
                _comment(101),
            ],
            headers={
                "Link": (
                    "<https://api.github.com/repos/org/repo/"
                    'issues/10/comments?page=3>; rel="next"'
                ),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_issue_comments_page(
            client,
            "org/repo",
            10,
            page=2,
            per_page=50,
        )

    assert len(result.comments) == 2
    assert result.next_page == 3

    first, second = result.comments

    assert first.repository == "org/repo"
    assert first.issue_number == 10
    assert first.id == 100
    assert first.body == "Discussion 100"
    assert first.user is not None
    assert first.user.login == "maintainer"

    assert second.id == 101
    assert second.user is None

    request = requests[0]
    assert request.url.path == "/repos/org/repo/issues/10/comments"
    assert request.url.params["page"] == "2"
    assert request.url.params["per_page"] == "50"
    assert request.headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION


async def test_empty_comments_page_has_no_continuation() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(200, json=[])
            )
        )
    ) as client:
        result = await fetch_issue_comments_page(
            client,
            "org/repo",
            10,
        )

    assert result.comments == ()
    assert result.next_page is None


async def test_fetch_merged_pull_request_details() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        requests.append(request)
        return httpx.Response(200, json=_pull_request(20))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_pull_request_details(
            client,
            "org/repo",
            20,
        )

    assert result.repository == "org/repo"
    assert result.number == 20
    assert result.merged is True
    assert result.merged_at is not None
    assert result.merge_commit_sha == "c" * 40
    assert result.head.sha == "a" * 40
    assert result.base.sha == "b" * 40
    assert result.changed_files == 5

    assert requests[0].url.path == "/repos/org/repo/pulls/20"
    assert requests[0].headers["X-GitHub-Api-Version"] == (GITHUB_API_VERSION)


async def test_unmerged_pr_may_have_merge_commit_sha() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(
                    200,
                    json=_pull_request(21, merged=False),
                )
            )
        )
    ) as client:
        result = await fetch_pull_request_details(
            client,
            "org/repo",
            21,
        )

    assert result.merged is False
    assert result.merged_at is None
    assert result.merge_commit_sha == "c" * 40


@pytest.mark.parametrize(
    ("repository", "number"),
    [
        ("org", 10),
        ("org//repo", 10),
        ("../repo", 10),
        ("org/repo", 0),
        ("org/repo", -1),
        ("org/repo", True),
    ],
)
async def test_invalid_identity_rejected_before_network(
    repository: str,
    number: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        raise AssertionError("Network request should not be made")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await fetch_issue_comments_page(
                client,
                repository,
                number,
            )

        with pytest.raises(ValueError):
            await fetch_pull_request_details(
                client,
                repository,
                number,
            )


@pytest.mark.parametrize(
    ("page", "per_page"),
    [
        (0, 100),
        (True, 100),
        (1, 0),
        (1, 101),
        (1, True),
    ],
)
async def test_invalid_comment_pagination_rejected(
    page: int,
    per_page: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        raise AssertionError("Network request should not be made")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await fetch_issue_comments_page(
                client,
                "org/repo",
                10,
                page=page,
                per_page=per_page,
            )


async def test_comment_http_error_propagates() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(503)
            )
        )
    ) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await fetch_issue_comments_page(
                client,
                "org/repo",
                10,
            )

    assert error.value.response.status_code == 503


async def test_pull_request_http_error_propagates() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(404)
            )
        )
    ) as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await fetch_pull_request_details(
                client,
                "org/repo",
                20,
            )

    assert error.value.response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "unexpected"},
        [None],
        [{"id": 1}],
    ],
)
async def test_invalid_comment_response_rejected(payload) -> None:
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
            await fetch_issue_comments_page(
                client,
                "org/repo",
                10,
            )


async def test_wrong_pull_request_number_rejected() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(
                    200,
                    json=_pull_request(99),
                )
            )
        )
    ) as client:
        with pytest.raises(ValueError, match="Unexpected"):
            await fetch_pull_request_details(
                client,
                "org/repo",
                20,
            )


async def test_invalid_pull_request_schema_rejected() -> None:
    payload = _pull_request(20)
    payload["head"] = {"sha": "invalid"}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(200, json=payload)
            )
        )
    ) as client:
        with pytest.raises(ValidationError):
            await fetch_pull_request_details(
                client,
                "org/repo",
                20,
            )
