"""Tests for bounded GitHub history collection."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from maintainer_lens.services.repository_history_collection import (
    collect_repository_history,
)

NEXT_PAGE_LINK = '<https://api.github.com/repos/org/repo/issues?page=2>; rel="next"'


def _issue(number: int, *, is_pr: bool = False) -> dict:
    item = {
        "number": number,
        "title": f"Topic {number}",
        "body": "A reported problem",
        "state": "closed" if is_pr else "open",
        "html_url": (
            f"https://github.com/org/repo/{'pull' if is_pr else 'issues'}/{number}"
        ),
        "created_at": "2026-10-23T01:00:00Z",
        "updated_at": "2026-10-24T01:00:00Z",
    }
    if is_pr:
        item["pull_request"] = {}
    return item


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_collects_multiple_pages_and_deduplicates_numbers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        requests.append(request)
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(
                200,
                json=[_issue(9), _issue(8, is_pr=True)],
                headers={"Link": NEXT_PAGE_LINK},
            )
        assert page == 2
        return httpx.Response(200, json=[_issue(8, is_pr=True), _issue(7)])

    since = datetime(2026, 10, 24, 8, tzinfo=timezone(timedelta(hours=8)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await collect_repository_history(
            client,
            "org/repo",
            per_page=2,
            max_pages=4,
            max_items=10,
            updated_since=since,
        )

    assert [(item.number, item.kind) for item in batch.items] == [
        (9, "issue"),
        (8, "pull_request"),
        (7, "issue"),
    ]
    assert batch.pages_fetched == 2
    assert batch.stop_reason == "exhausted"
    assert len(requests) == 2
    assert [request.url.params["per_page"] for request in requests] == ["2", "2"]
    assert [request.url.params["since"] for request in requests] == [
        "2026-10-24T00:00:00Z",
        "2026-10-24T00:00:00Z",
    ]


@pytest.mark.anyio
async def test_stops_at_page_limit_without_requesting_another_page() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        requests.append(request)
        return httpx.Response(
            200,
            json=[_issue(3)],
            headers={"Link": NEXT_PAGE_LINK},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await collect_repository_history(
            client, "org/repo", max_pages=1, max_items=10
        )

    assert batch.stop_reason == "page_limit"
    assert batch.pages_fetched == 1
    assert [item.number for item in batch.items] == [3]
    assert len(requests) == 1


@pytest.mark.anyio
async def test_stops_in_the_middle_of_a_page_at_item_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        requests.append(request)
        return httpx.Response(
            200,
            json=[_issue(5), _issue(4), _issue(3)],
            headers={"Link": NEXT_PAGE_LINK},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await collect_repository_history(
            client, "org/repo", per_page=3, max_items=2
        )

    assert batch.stop_reason == "item_limit"
    assert batch.pages_fetched == 1
    assert [item.number for item in batch.items] == [5, 4]
    assert len(requests) == 1


@pytest.mark.anyio
async def test_exact_item_limit_on_last_page_is_exhausted() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"private": False})
                if request.url.path.count("/") == 3
                else httpx.Response(200, json=[_issue(5), _issue(4)])
            )
        )
    ) as client:
        batch = await collect_repository_history(client, "org/repo", max_items=2)

    assert batch.stop_reason == "exhausted"
    assert batch.pages_fetched == 1
    assert len(batch.items) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("per_page", "max_pages", "max_items"),
    [
        (0, 2, 10),
        (101, 2, 10),
        (True, 2, 10),
        (10, 0, 10),
        (10, True, 10),
        (10, 2, 0),
        (10, 2, False),
    ],
)
async def test_invalid_limits_make_no_http_requests(
    per_page: int, max_pages: int, max_items: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        raise AssertionError("HTTP request must not happen")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await collect_repository_history(
                client,
                "org/repo",
                per_page=per_page,
                max_pages=max_pages,
                max_items=max_items,
            )


@pytest.mark.anyio
async def test_http_failure_propagates_instead_of_returning_partial_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.count("/") == 3:
            return httpx.Response(200, json={"private": False})
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=[_issue(8)],
                headers={"Link": NEXT_PAGE_LINK},
            )
        return httpx.Response(503, json={"message": "Service unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await collect_repository_history(client, "org/repo")

    assert exc.value.response.status_code == 503
