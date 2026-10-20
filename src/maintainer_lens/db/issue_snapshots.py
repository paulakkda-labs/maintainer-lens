"""Persistence operations for investigation issue snapshots."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationTask,
)


@dataclass(frozen=True, slots=True)
class IssueSnapshotData:
    """Validated issue data to persist for an investigation."""

    title: str
    body: str | None
    state: Literal["open", "closed"]
    source_url: str
    source_created_at: datetime
    source_updated_at: datetime


async def upsert_issue_snapshot(
    session: AsyncSession,
    *,
    task_id: str,
    claim_token: str,
    snapshot: IssueSnapshotData,
) -> bool:
    """Upsert a snapshot only while the worker owns a valid task lease."""

    locked_task_id = await session.scalar(
        select(InvestigationTask.id)
        .where(
            InvestigationTask.id == task_id,
            InvestigationTask.status == "running",
            InvestigationTask.claim_token == claim_token,
            InvestigationTask.lease_expires_at > func.clock_timestamp(),
        )
        .with_for_update()
    )

    if locked_task_id is None:
        return False

    stmt = insert(InvestigationIssueSnapshot).values(
        task_id=task_id,
        title=snapshot.title,
        body=snapshot.body,
        state=snapshot.state,
        source_url=snapshot.source_url,
        source_created_at=snapshot.source_created_at,
        source_updated_at=snapshot.source_updated_at,
        fetched_at=func.clock_timestamp(),
    )

    stmt = stmt.on_conflict_do_update(
        index_elements=[InvestigationIssueSnapshot.task_id],
        set_={
            "title": stmt.excluded.title,
            "body": stmt.excluded.body,
            "state": stmt.excluded.state,
            "source_url": stmt.excluded.source_url,
            "source_created_at": stmt.excluded.source_created_at,
            "source_updated_at": stmt.excluded.source_updated_at,
            "fetched_at": stmt.excluded.fetched_at,
        },
    )

    await session.execute(stmt)

    return True
