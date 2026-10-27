"""Tests for bounded GitHub history enrichment."""

import httpx
import pytest

from maintainer_lens.clients.github_history import GitHubHistoryItem
from maintainer_lens.services.repository_history_enrichment import (
    collect_repository_history_enrichment,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _item(number: int, *, is_pr: bool = False) -> GitHubHistoryItem:
    kind = "pull_request" if is_pr else "issue"
    path = "pull" if is_pr else "issues"

    return GitHubHistoryItem.model_validate(
        {
            "repository": "org/repo",
            "number": number,
            "kind": kind,
            "title": f"Topic {number}",
            "body": "History description",
            "state": "closed" if is_pr else "open",
            "html_url": f"https://github.com/org/repo/{path}/{number}",
            "created_at": "2026-10-20T00:00:00Z",
            "updated_at": "2026-10-25T00:00:00Z",
        }
    )


def _comment(comment_id: int, issue_number: int) -> dict:
    return {
        "id": comment_id,
        "body": f"Comment {comment_id}",
        "html_url": (
            "https://github.com/org/repo/issues/"
            f"{issue_number}#issuecomment-{comment_id}"
        ),
        "created_at": "2026-10-20T01:00:00Z",
        "updated_at": "2026-10-21T01:00:00Z",
        "user": {"login": "maintainer"},
    }


def _pr_details() -> dict:
    return {
        "number": 2,
        "html_url": "https://github.com/org/repo/pull/2",
        "merged": True,
        "merged_at": "2026-10-22T01:00:00Z",
        "merge_commit_sha": "c" * 40,
        "head": {"sha": "a" * 40},
        "base": {"sha": "b" * 40},
        "changed_files": 3,
    }


async def test_bounded_collection_keeps_incomplete_comment_status() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        requested.append(str(request.url))
        path = request.url.path
        page = int(request.url.params.get("page", "1"))

        if path.endswith("/issues/1/comments"):
            headers = {
                "Link": (
                    "<https://api.github.com/repos/org/repo/"
                    f'issues/1/comments?page={page + 1}>; rel="next"'
                )
            }
            return httpx.Response(
                200,
                json=[_comment(100 + page, 1)],
                headers=headers,
            )

        if path.endswith("/issues/2/comments"):
            return httpx.Response(200, json=[])

        if path.endswith("/pulls/2"):
            return httpx.Response(200, json=_pr_details())

        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collect_repository_history_enrichment(
            client,
            "org/repo",
            [_item(1), _item(2, is_pr=True), _item(3)],
            max_items=2,
            max_comment_pages=2,
            comments_per_page=10,
        )

    assert [item.number for item in result.items] == [1, 2]
    assert [comment.id for comment in result.comments] == [101, 102]
    assert [pr.number for pr in result.pull_requests] == [2]
    assert result.incomplete_comment_numbers == (1,)
    assert result.unprocessed_items == 1
    assert result.requests_made == 8
    assert len(requested) == 4
    assert all("/issues/3/" not in url for url in requested)


async def test_invalid_budget_is_rejected_before_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        raise AssertionError("Network must not be requested")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="max_items"):
            await collect_repository_history_enrichment(
                client,
                "org/repo",
                [_item(1)],
                max_items=0,
            )

        with pytest.raises(ValueError, match="max_comment_pages"):
            await collect_repository_history_enrichment(
                client,
                "org/repo",
                [_item(1)],
                max_comment_pages=4,
            )

        with pytest.raises(ValueError, match="Duplicate"):
            await collect_repository_history_enrichment(
                client,
                "org/repo",
                [_item(1), _item(1)],
            )


async def test_github_failure_propagates() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(503)
            )
        )
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await collect_repository_history_enrichment(
                client,
                "org/repo",
                [_item(1)],
            )
