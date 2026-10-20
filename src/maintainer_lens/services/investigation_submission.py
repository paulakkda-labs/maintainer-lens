"""Investigation submission and transactional outbox creation."""

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.messaging.schemas import (
    INVESTIGATION_REQUESTED_EVENT_TYPE,
    InvestigationRequestedMessage,
)


async def create_investigation_submission(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repository: str,
    issue_number: int,
) -> InvestigationRequestedMessage:
    """Atomically create an investigation task and its outbox event."""

    message = InvestigationRequestedMessage(
        task_id=str(uuid4()),
        repository=repository,
        issue_number=issue_number,
    )

    task = InvestigationTask(
        id=message.task_id,
        repository=message.repository,
        issue_number=message.issue_number,
        status="pending",
    )

    outbox_event = InvestigationOutboxEvent(
        task_id=message.task_id,
        event_type=INVESTIGATION_REQUESTED_EVENT_TYPE,
        payload=message.model_dump(mode="json"),
    )

    async with session_factory() as session:
        async with session.begin():
            session.add(task)
            await session.flush()

            session.add(outbox_event)

    return message
