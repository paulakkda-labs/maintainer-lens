"""Read investigation artifacts with their authoritative task status."""

from sqlalchemy import func, select, tuple_
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


async def find_investigation_tasks(
    session: AsyncSession,
    *,
    repository: str,
    issue_number: int,
    before: str | None = None,
    limit: int = 20,
) -> tuple[list[InvestigationTask], str | None]:
    """Read all submission modes/profiles, newest first, without URL arbitration.

    The caller owns the read Session. A task ID is a repository/Issue-scoped
    cursor for the immutable (created_at, id) ordering, not a freshness check.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid investigation page limit")
    identity = (
        func.lower(InvestigationTask.repository) == repository.casefold(),
        InvestigationTask.issue_number == issue_number,
    )
    statement = select(InvestigationTask).where(*identity)
    if before is not None:
        cursor = (
            await session.execute(
                select(InvestigationTask.created_at, InvestigationTask.id).where(
                    *identity, InvestigationTask.id == before
                )
            )
        ).one_or_none()
        if cursor is None:
            raise ValueError("Invalid investigation page cursor")
        statement = statement.where(
            tuple_(InvestigationTask.created_at, InvestigationTask.id)
            < tuple_(cursor.created_at, cursor.id)
        )
    rows = (
        await session.scalars(
            statement.order_by(
                InvestigationTask.created_at.desc(), InvestigationTask.id.desc()
            )
            .limit(limit + 1)
            .execution_options(populate_existing=True)
        )
    ).all()
    selected = list(rows[:limit])
    return selected, selected[-1].id if len(rows) > limit else None
