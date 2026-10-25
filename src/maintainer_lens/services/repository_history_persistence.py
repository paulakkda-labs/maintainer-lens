"""Persist repository history batches with monotonic source updates."""

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.services.repository_history_collection import (
    RepositoryHistoryBatch,
)


async def write_repository_history_batch(
    session: AsyncSession,
    repository: str,
    batch: RepositoryHistoryBatch,
) -> None:
    """Upsert history records within a caller-managed transaction."""

    if not repository.strip():
        raise ValueError("Repository must not be blank")

    rows: list[dict] = []
    seen_numbers: set[int] = set()

    for item in batch.items:
        if item.repository != repository:
            raise ValueError("History item belongs to another repository")

        if item.number in seen_numbers:
            raise ValueError("Duplicate history number in batch")

        seen_numbers.add(item.number)

        if item.created_at.utcoffset() is None or item.updated_at.utcoffset() is None:
            raise ValueError("GitHub history timestamps must be timezone-aware")

        rows.append(
            {
                "repository": repository,
                "number": item.number,
                "kind": item.kind,
                "title": item.title,
                "body": item.body,
                "state": item.state,
                "source_url": str(item.html_url),
                "source_created_at": item.created_at,
                "source_updated_at": item.updated_at,
            }
        )

    if not rows:
        return

    statement = insert(RepositoryHistoryRecord).values(rows)

    statement = statement.on_conflict_do_update(
        index_elements=[
            RepositoryHistoryRecord.repository,
            RepositoryHistoryRecord.number,
        ],
        set_={
            "kind": statement.excluded.kind,
            "title": statement.excluded.title,
            "body": statement.excluded.body,
            "state": statement.excluded.state,
            "source_url": statement.excluded.source_url,
            "source_created_at": statement.excluded.source_created_at,
            "source_updated_at": statement.excluded.source_updated_at,
            "fetched_at": func.clock_timestamp(),
        },
        where=(
            statement.excluded.source_updated_at
            >= RepositoryHistoryRecord.source_updated_at
        ),
    )

    await session.execute(statement)


async def save_repository_history_batch(
    session: AsyncSession,
    repository: str,
    batch: RepositoryHistoryBatch,
) -> None:
    """Upsert and commit one standalone history batch."""

    async with session.begin():
        await write_repository_history_batch(
            session,
            repository,
            batch,
        )
