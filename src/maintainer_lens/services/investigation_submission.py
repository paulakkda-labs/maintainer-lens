"""Investigation submission and transactional outbox creation."""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.messaging.schemas import (
    INVESTIGATION_REQUESTED_EVENT_TYPE,
    InvestigationRequestedMessage,
)
from maintainer_lens.schemas.investigation_budget import (
    KnowledgeBudgetProfile,
    KnowledgePreparationBudget,
    make_knowledge_preparation_budget,
)


async def add_investigation_submission(
    session: AsyncSession,
    *,
    repository: str,
    issue_number: int,
    budget: KnowledgePreparationBudget,
    source_index_id: UUID | None = None,
    auto_prepare_knowledge: bool = False,
) -> InvestigationRequestedMessage:
    """Stage Task and Outbox in the caller's transaction; do not commit here."""
    message = InvestigationRequestedMessage(
        task_id=str(uuid4()),
        repository=repository,
        issue_number=issue_number,
        source_index_id=source_index_id,
        auto_prepare_knowledge=auto_prepare_knowledge,
    )
    session.add(
        InvestigationTask(
            id=message.task_id,
            repository=message.repository,
            issue_number=message.issue_number,
            source_index_id=message.source_index_id,
            auto_prepare_knowledge=message.auto_prepare_knowledge,
            knowledge_budget=budget.model_dump(mode="json"),
            status="pending",
            execution_generation=0,
            failure_retryable=False,
        )
    )
    await session.flush()
    session.add(
        InvestigationOutboxEvent(
            task_id=message.task_id,
            generation=0,
            event_type=INVESTIGATION_REQUESTED_EVENT_TYPE,
            payload=message.model_dump(mode="json"),
        )
    )
    return message


async def create_investigation_submission(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository: str,
    issue_number: int,
    source_index_id: UUID | None = None,
    knowledge_budget_profile: KnowledgeBudgetProfile = "standard",
    auto_prepare_knowledge: bool = False,
) -> InvestigationRequestedMessage:
    """Atomically create an investigation task and its outbox event."""
    budget = make_knowledge_preparation_budget(knowledge_budget_profile)
    async with session_factory() as session, session.begin():
        return await add_investigation_submission(
            session,
            repository=repository,
            issue_number=issue_number,
            budget=budget,
            source_index_id=source_index_id,
            auto_prepare_knowledge=auto_prepare_knowledge,
        )
