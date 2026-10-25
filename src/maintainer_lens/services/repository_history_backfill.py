"""Run bounded and resumable repository history backfills."""

from dataclasses import dataclass
from typing import Literal

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.github_history import fetch_repository_history_page
from maintainer_lens.db.models.repository_history_backfill import (
    RepositoryHistoryBackfillState,
)
from maintainer_lens.db.models.repository_history_sync import (
    RepositoryHistorySyncState,
)
from maintainer_lens.services.repository_history_collection import (
    RepositoryHistoryBatch,
)
from maintainer_lens.services.repository_history_persistence import (
    write_repository_history_batch,
)


class RepositoryHistoryBackfillConflict(RuntimeError):
    """The stored backfill cursor changed during collection."""


@dataclass(frozen=True, slots=True)
class RepositoryHistoryBackfillResult:
    """Outcome of one bounded backfill run."""

    status: Literal["partial", "completed", "already_completed"]
    pages_fetched: int
    items_fetched: int
    next_page: int | None


async def backfill_repository_history(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    repository: str,
    *,
    per_page: int = 100,
    max_pages: int = 2,
) -> RepositoryHistoryBackfillResult:
    """Persist complete pages and atomically advance the backfill cursor."""

    if not repository.strip():
        raise ValueError("Repository must not be blank")
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100")
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise ValueError("max_pages must be between 1 and 10")

    # Create the initial cursor, or load the existing one.
    async with session_factory() as session:
        async with session.begin():
            initial_state = insert(RepositoryHistoryBackfillState).values(
                repository=repository,
                next_page=1,
                per_page=per_page,
                started_at=func.clock_timestamp(),
                completed_at=None,
            )

            initial_state = initial_state.on_conflict_do_nothing(
                index_elements=[RepositoryHistoryBackfillState.repository],
            )

            await session.execute(initial_state)

            state = await session.get(
                RepositoryHistoryBackfillState,
                repository,
            )

            if state is None:
                raise RuntimeError("Could not initialize backfill state")

            if state.per_page != per_page:
                raise ValueError("Cannot change per_page during backfill")

            start_page = state.next_page

            if state.completed_at is not None:
                return RepositoryHistoryBackfillResult(
                    status="already_completed",
                    pages_fetched=0,
                    items_fetched=0,
                    next_page=None,
                )

    # Do not hold a database transaction during GitHub API requests.
    current_page = start_page
    next_page: int | None = start_page
    pages_fetched = 0
    complete = False

    items = []
    seen_numbers: set[int] = set()

    for _ in range(max_pages):
        page = await fetch_repository_history_page(
            http_client,
            repository,
            page=current_page,
            per_page=per_page,
            sort_by="created",
            direction="asc",
        )

        pages_fetched += 1

        for item in page.items:
            if item.number not in seen_numbers:
                items.append(item)
                seen_numbers.add(item.number)

        if page.next_page is None:
            complete = True
            next_page = None
            break

        if page.next_page != current_page + 1:
            raise ValueError("Unexpected GitHub pagination sequence")

        current_page = page.next_page
        next_page = current_page

    batch = RepositoryHistoryBatch(
        items=tuple(items),
        pages_fetched=pages_fetched,
        stop_reason="exhausted" if complete else "page_limit",
    )

    # History records and cursor changes must commit together.
    async with session_factory() as session:
        async with session.begin():
            state = await session.scalar(
                select(RepositoryHistoryBackfillState)
                .where(RepositoryHistoryBackfillState.repository == repository)
                .with_for_update()
            )

            if (
                state is None
                or state.completed_at is not None
                or state.next_page != start_page
            ):
                raise RepositoryHistoryBackfillConflict(
                    "Backfill cursor changed; retry with the current state"
                )

            await write_repository_history_batch(
                session,
                repository,
                batch,
            )

            if complete:
                state.completed_at = await session.scalar(
                    select(func.clock_timestamp())
                )

                # Start later incremental scans from the backfill start time.
                checkpoint = insert(RepositoryHistorySyncState).values(
                    repository=repository,
                    completed_through=state.started_at,
                    last_attempt_at=func.clock_timestamp(),
                    last_stop_reason="exhausted",
                    last_pages_fetched=pages_fetched,
                    last_item_count=len(items),
                )

                checkpoint = checkpoint.on_conflict_do_update(
                    index_elements=[RepositoryHistorySyncState.repository],
                    set_={
                        "completed_through": func.greatest(
                            RepositoryHistorySyncState.completed_through,
                            checkpoint.excluded.completed_through,
                        ),
                    },
                )

                await session.execute(checkpoint)

            else:
                if next_page is None:
                    raise RuntimeError("Missing continuation page")

                state.next_page = next_page

    return RepositoryHistoryBackfillResult(
        status="completed" if complete else "partial",
        pages_fetched=pages_fetched,
        items_fetched=len(items),
        next_page=next_page,
    )
