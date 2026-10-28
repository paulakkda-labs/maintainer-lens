"""Read investigation artifacts with their authoritative task status."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord


async def get_investigation_details(
    session: AsyncSession,
    task_id: str,
) -> (
    tuple[
        InvestigationTask,
        InvestigationIssueSnapshot | None,
        InvestigationResultRecord | None,
    ]
    | None
):
    """Read status, snapshot and result in one statement without locking/writing.

    Result presence alone does not establish completion. The caller must gate
    result decoding/exposure on the returned task's succeeded status.
    """

    stmt = (
        select(InvestigationTask, InvestigationIssueSnapshot, InvestigationResultRecord)
        .outerjoin(
            InvestigationIssueSnapshot,
            InvestigationIssueSnapshot.task_id == InvestigationTask.id,
        )
        .outerjoin(
            InvestigationResultRecord,
            InvestigationResultRecord.task_id == InvestigationTask.id,
        )
        .where(InvestigationTask.id == task_id)
        .execution_options(populate_existing=True)
    )

    result = await session.execute(stmt)
    row = result.one_or_none()

    if row is None:
        return None

    task, snapshot, stored_result = row
    return task, snapshot, stored_result
