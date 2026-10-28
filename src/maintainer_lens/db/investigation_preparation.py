"""Read preparation snapshots and fence the backend-selected Source identity."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.schemas.investigation_budget import KnowledgePreparationBudget


@dataclass(frozen=True, slots=True)
class ClaimedKnowledgePreparation:
    repository: str
    issue_number: int
    prepared_source_index_id: UUID | None
    budget: KnowledgePreparationBudget


def effective_source_index_expression():
    """Use the prepared identity only for tasks that requested automatic mode."""
    return case(
        (
            InvestigationTask.auto_prepare_knowledge,
            InvestigationTask.prepared_source_index_id,
        ),
        else_=InvestigationTask.source_index_id,
    )


async def get_claimed_knowledge_preparation(
    session: AsyncSession, *, task_id: str, claim_token: str
) -> ClaimedKnowledgePreparation | None:
    """Validate the stored snapshot under a short read transaction owned by caller."""
    row = (
        await session.execute(
            select(
                InvestigationTask.repository,
                InvestigationTask.issue_number,
                InvestigationTask.prepared_source_index_id,
                InvestigationTask.knowledge_budget,
            ).where(
                InvestigationTask.id == task_id,
                InvestigationTask.auto_prepare_knowledge.is_(True),
                InvestigationTask.source_index_id.is_(None),
                InvestigationTask.status == "running",
                InvestigationTask.claim_token == claim_token,
                InvestigationTask.lease_expires_at > func.clock_timestamp(),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return ClaimedKnowledgePreparation(
        repository=row.repository,
        issue_number=row.issue_number,
        prepared_source_index_id=row.prepared_source_index_id,
        budget=KnowledgePreparationBudget.model_validate(row.knowledge_budget),
    )


async def bind_prepared_source_index(
    session: AsyncSession,
    *,
    task_id: str,
    claim_token: str,
    repository: str,
    issue_number: int,
    source_index_id: UUID,
) -> bool:
    """Bind once, checking database time after any row-lock wait; caller commits.

    Rebinding the same UUID is idempotent. Another UUID, mode, task identity or
    expired/replaced claim cannot change this task. Source metadata is validated
    by the service before calling this write boundary.
    """
    if not isinstance(source_index_id, UUID):
        raise ValueError("Prepared Source index must be a UUID")
    await session.execute(
        select(InvestigationTask.id)
        .where(InvestigationTask.id == task_id)
        .with_for_update()
    )
    result = await session.scalar(
        update(InvestigationTask)
        .where(
            InvestigationTask.id == task_id,
            InvestigationTask.repository == repository,
            InvestigationTask.issue_number == issue_number,
            InvestigationTask.auto_prepare_knowledge.is_(True),
            InvestigationTask.source_index_id.is_(None),
            InvestigationTask.prepared_source_index_id.is_(None)
            | (InvestigationTask.prepared_source_index_id == source_index_id),
            InvestigationTask.status == "running",
            InvestigationTask.claim_token == claim_token,
            InvestigationTask.lease_expires_at > func.clock_timestamp(),
        )
        .values(prepared_source_index_id=source_index_id)
        .returning(InvestigationTask.id)
    )
    return result is not None
