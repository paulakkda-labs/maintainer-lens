"""Queries for investigation details and issue snapshots."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationTask,
)


async def get_investigation_with_snapshot(
    session: AsyncSession,
    task_id: str,
) -> tuple[InvestigationTask, InvestigationIssueSnapshot | None] | None:
    """Return a task and its optional issue snapshot."""

    stmt = (
        select(InvestigationTask, InvestigationIssueSnapshot)
        .outerjoin(
            InvestigationIssueSnapshot,
            InvestigationIssueSnapshot.task_id == InvestigationTask.id,
        )
        .where(InvestigationTask.id == task_id)
    )

    result = await session.execute(stmt)
    row = result.one_or_none()

    if row is None:
        return None

    task, snapshot = row
    return task, snapshot
