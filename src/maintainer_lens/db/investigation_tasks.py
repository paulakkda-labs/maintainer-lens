from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.investigation import InvestigationTask

LEASE_DURATION = timedelta(minutes=15)


async def create_investigation_task(
    session: AsyncSession,
    task_id: str,
    repository: str,
    issue_number: int,
    response_language: str = "en",
) -> InvestigationTask:
    """Create a pending investigation task."""
    task = InvestigationTask(
        id=task_id,
        repository=repository,
        issue_number=issue_number,
        response_language=response_language,
    )
    session.add(task)
    await session.flush()
    return task


async def claim_investigation_task(
    session: AsyncSession,
    task_id: str,
    expected_generation: int = 0,
) -> str | None:
    """Atomically claim a pending or expired investigation task."""
    if type(expected_generation) is not int or expected_generation < 0:
        raise ValueError("Invalid execution generation")
    claim_token = uuid4().hex

    expired_running = and_(
        InvestigationTask.status == "running",
        InvestigationTask.lease_expires_at <= func.statement_timestamp(),
    )

    claimable = or_(
        InvestigationTask.status == "pending",
        expired_running,
    )
    stmt = (
        update(InvestigationTask)
        .where(
            InvestigationTask.id == task_id,
            InvestigationTask.execution_generation == expected_generation,
            claimable,
        )
        .values(
            status="running",
            claim_token=claim_token,
            lease_expires_at=func.statement_timestamp() + LEASE_DURATION,
        )
        .returning(InvestigationTask.claim_token)
    )
    result = await session.execute(stmt)
    claim_token = result.scalar_one_or_none()
    return claim_token


async def get_investigation_task_generation(
    session: AsyncSession,
    task_id: str,
) -> int | None:
    return await session.scalar(
        select(InvestigationTask.execution_generation).where(
            InvestigationTask.id == task_id
        )
    )


async def get_claimed_investigation_identity(
    session: AsyncSession,
    *,
    task_id: str,
    claim_token: str,
    auto_prepare_knowledge: bool = False,
    response_language: str = "zh",
) -> tuple[str, int, UUID | None] | None:
    """Read identity for an active claim without holding a row lock.

    This preflight does not authorize later writes; each writer must fence
    again. The caller owns the short read transaction.
    """
    result = await session.execute(
        select(
            InvestigationTask.repository,
            InvestigationTask.issue_number,
            InvestigationTask.source_index_id,
            InvestigationTask.response_language,
        ).where(
            InvestigationTask.id == task_id,
            InvestigationTask.status == "running",
            InvestigationTask.claim_token == claim_token,
            InvestigationTask.auto_prepare_knowledge.is_(auto_prepare_knowledge),
            InvestigationTask.lease_expires_at > func.clock_timestamp(),
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    if row.response_language != response_language:
        raise ValueError("Investigation message language differs from its task")
    return row.repository, row.issue_number, row.source_index_id


async def mark_investigation_succeeded(
    session: AsyncSession,
    task_id: str,
    claim_token: str,
) -> bool:
    """Mark an investigation as succeeded if the claim is still valid."""
    stmt = (
        update(InvestigationTask)
        .where(
            InvestigationTask.id == task_id,
            InvestigationTask.status == "running",
            InvestigationTask.claim_token == claim_token,
            InvestigationTask.lease_expires_at > func.statement_timestamp(),
        )
        .values(
            status="succeeded",
            claim_token=None,
            lease_expires_at=None,
        )
        .returning(InvestigationTask.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def mark_investigation_failed(
    session: AsyncSession,
    task_id: str,
    claim_token: str,
) -> bool:
    """Atomically mark a running investigation task as failed."""
    stmt = (
        update(InvestigationTask)
        .where(
            InvestigationTask.id == task_id,
            InvestigationTask.status == "running",
            InvestigationTask.claim_token == claim_token,
            InvestigationTask.lease_expires_at > func.statement_timestamp(),
        )
        .values(
            status="failed",
            claim_token=None,
            lease_expires_at=None,
        )
        .returning(InvestigationTask.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def renew_investigation_lease(
    session: AsyncSession,
    task_id: str,
    claim_token: str,
) -> bool:
    """Extend an active investigation lease owned by the current worker."""
    stmt = (
        update(InvestigationTask)
        .where(
            InvestigationTask.id == task_id,
            InvestigationTask.status == "running",
            InvestigationTask.claim_token == claim_token,
            InvestigationTask.lease_expires_at > func.statement_timestamp(),
        )
        .values(
            lease_expires_at=func.statement_timestamp() + LEASE_DURATION,
        )
        .returning(InvestigationTask.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def get_investigation_task_status(
    session: AsyncSession,
    task_id: str,
) -> str | None:
    """Return the persisted status of an investigation task."""
    stmt = select(InvestigationTask.status).where(InvestigationTask.id == task_id)
    return await session.scalar(stmt)


async def release_investigation_task_for_retry(
    session: AsyncSession,
    task_id: str,
    claim_token: str,
) -> bool:
    """Release an active investigation claim for a future retry."""
    stmt = (
        update(InvestigationTask)
        .where(
            InvestigationTask.id == task_id,
            InvestigationTask.status == "running",
            InvestigationTask.claim_token == claim_token,
            InvestigationTask.lease_expires_at > func.statement_timestamp(),
        )
        .values(
            status="pending",
            claim_token=None,
            lease_expires_at=None,
        )
        .returning(InvestigationTask.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None
