"""Collect a bounded batch of repository issue and pull-request history."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx

from maintainer_lens.clients.github_history import (
    GitHubHistoryItem,
    fetch_repository_history_page,
)

HistoryStopReason = Literal["exhausted", "item_limit", "page_limit"]


@dataclass(frozen=True, slots=True)
class RepositoryHistoryBatch:
    """History items and the reason this collection run stopped."""

    items: tuple[GitHubHistoryItem, ...]
    pages_fetched: int
    stop_reason: HistoryStopReason


async def collect_repository_history(
    http_client: httpx.AsyncClient,
    repository: str,
    *,
    per_page: int = 100,
    max_pages: int = 3,
    max_items: int = 200,
    updated_since: datetime | None = None,
) -> RepositoryHistoryBatch:
    """Fetch a bounded, deduplicated batch without claiming a durable sync."""

    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100")
    if type(max_pages) is not int or max_pages <= 0:
        raise ValueError("max_pages must be a positive integer")
    if type(max_items) is not int or max_items <= 0:
        raise ValueError("max_items must be a positive integer")

    items: list[GitHubHistoryItem] = []
    seen_numbers: set[int] = set()
    current_page = 1

    for pages_fetched in range(1, max_pages + 1):
        result = await fetch_repository_history_page(
            http_client,
            repository,
            page=current_page,
            per_page=per_page,
            updated_since=updated_since,
        )

        for item in result.items:
            if item.number in seen_numbers:
                continue
            if len(items) >= max_items:
                return RepositoryHistoryBatch(
                    items=tuple(items),
                    pages_fetched=pages_fetched,
                    stop_reason="item_limit",
                )
            items.append(item)
            seen_numbers.add(item.number)

        if result.next_page is None:
            return RepositoryHistoryBatch(
                items=tuple(items),
                pages_fetched=pages_fetched,
                stop_reason="exhausted",
            )

        if result.next_page <= current_page:
            raise ValueError("GitHub history next_page must increase")

        if len(items) >= max_items:
            return RepositoryHistoryBatch(
                items=tuple(items),
                pages_fetched=pages_fetched,
                stop_reason="item_limit",
            )

        current_page = result.next_page

    return RepositoryHistoryBatch(
        items=tuple(items),
        pages_fetched=max_pages,
        stop_reason="page_limit",
    )
