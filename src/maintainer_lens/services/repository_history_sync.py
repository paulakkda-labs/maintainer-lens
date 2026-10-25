"""Synchronize repository history with durable progress tracking."""

from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.models.repository_history_sync import (
    RepositoryHistorySyncState,
)
from maintainer_lens.services.repository_history_collection import (
    RepositoryHistoryBatch,
    collect_repository_history,
)
from maintainer_lens.services.repository_history_persistence import (
    write_repository_history_batch,
)


@dataclass(frozen=True, slots=True)
class RepositoryHistorySyncResult:
    """Summary of one repository history synchronization attempt."""

    batch: RepositoryHistoryBatch
    started_at: datetime
    requested_since: datetime | None


async def sync_repository_history(
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient,
    repository: str,
    *,
    per_page: int = 100,
    max_pages: int = 3,
    max_items: int = 200,
    overlap: timedelta = timedelta(days=1),
) -> RepositoryHistorySyncResult:
    """Collect and atomically persist history with its sync checkpoint."""

    if not repository.strip():
        raise ValueError("Repository must not be blank")

    if not isinstance(overlap, timedelta) or overlap <= timedelta(0):
        raise ValueError("overlap must be a positive timedelta")

    async with session_factory() as session:
        started_at = await session.scalar(select(func.clock_timestamp()))

        completed_through = await session.scalar(
            select(RepositoryHistorySyncState.completed_through).where(
                RepositoryHistorySyncState.repository == repository
            )
        )

    if started_at is None:
        raise RuntimeError("Could not determine database time")

    requested_since = (
        completed_through - overlap if completed_through is not None else None
    )

    batch = await collect_repository_history(
        http_client,
        repository,
        per_page=per_page,
        max_pages=max_pages,
        max_items=max_items,
        updated_since=requested_since,
    )

    checkpoint = started_at if batch.stop_reason == "exhausted" else None

    async with session_factory() as session:
        async with session.begin():
            await write_repository_history_batch(
                session,
                repository,
                batch,
            )

            statement = insert(RepositoryHistorySyncState).values(
                repository=repository,
                completed_through=checkpoint,
                last_attempt_at=started_at,
                last_stop_reason=batch.stop_reason,
                last_pages_fetched=batch.pages_fetched,
                last_item_count=len(batch.items),
            )

            is_latest_attempt = (
                statement.excluded.last_attempt_at
                >= RepositoryHistorySyncState.last_attempt_at
            )

            statement = statement.on_conflict_do_update(
                index_elements=[RepositoryHistorySyncState.repository],
                set_={
                    "completed_through": func.greatest(
                        RepositoryHistorySyncState.completed_through,
                        statement.excluded.completed_through,
                    ),
                    "last_attempt_at": func.greatest(
                        RepositoryHistorySyncState.last_attempt_at,
                        statement.excluded.last_attempt_at,
                    ),
                    "last_stop_reason": case(
                        (
                            is_latest_attempt,
                            statement.excluded.last_stop_reason,
                        ),
                        else_=RepositoryHistorySyncState.last_stop_reason,
                    ),
                    "last_pages_fetched": case(
                        (
                            is_latest_attempt,
                            statement.excluded.last_pages_fetched,
                        ),
                        else_=RepositoryHistorySyncState.last_pages_fetched,
                    ),
                    "last_item_count": case(
                        (
                            is_latest_attempt,
                            statement.excluded.last_item_count,
                        ),
                        else_=RepositoryHistorySyncState.last_item_count,
                    ),
                },
            )

            await session.execute(statement)

    return RepositoryHistorySyncResult(
        batch=batch,
        started_at=started_at,
        requested_since=requested_since,
    )
