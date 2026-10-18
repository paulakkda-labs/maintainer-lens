"""Message handling for investigation requests."""

from collections.abc import Awaitable, Callable

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustExchange
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_tasks import get_investigation_task_status
from maintainer_lens.investigation_runner import (
    InvestigationClaimUnavailableError,
    RetryableInvestigationError,
    execute_investigation_task,
)
from maintainer_lens.messaging.publisher import republish_investigation_message
from maintainer_lens.messaging.schemas import (
    MISSING_TASK_CHECKS_HEADER,
    RETRY_ATTEMPTS_HEADER,
    InvestigationRequestedMessage,
    parse_investigation_message,
)
from maintainer_lens.messaging.topology import DEAD_ROUTING_KEY, RETRY_ROUTING_KEY

MAX_MISSING_TASK_CHECKS = 3

MAX_INVESTIGATION_RETRIES = 3


class RetryableMessageError(RuntimeError):
    """Raised when a message requires a delayed retry."""

    def __init__(self, task_id: str, status: str | None) -> None:
        self.task_id = task_id
        self.status = status

        super().__init__(
            f"Task cannot be processed: task_id={task_id}, status={status}"
        )


async def handle_investigation_message(
    message: AbstractIncomingMessage,
    processor: Callable[[InvestigationRequestedMessage, bool], Awaitable[None]],
) -> None:
    """Validate and dispatch an investigation request for processing."""
    request = parse_investigation_message(message)
    await processor(request)


async def process_investigation_request(
    request: InvestigationRequestedMessage,
    session_factory: async_sessionmaker[AsyncSession],
    processor: Callable[[], Awaitable[None]],
    final_attempt: bool = False,
) -> None:
    """Execute an investigation or determine whether its message needs retrying."""
    try:
        await execute_investigation_task(
            session_factory=session_factory,
            task_id=request.task_id,
            processor=processor,
            final_attempt=final_attempt,
        )

    except InvestigationClaimUnavailableError as exc:
        async with session_factory() as session:
            status = await get_investigation_task_status(
                session=session,
                task_id=request.task_id,
            )

        if status == "succeeded":
            return

        raise RetryableMessageError(
            task_id=request.task_id,
            status=status,
        ) from exc


async def handle_investigation_message_with_retry(
    message: AbstractIncomingMessage,
    exchange: AbstractRobustExchange,
    processor: Callable[[InvestigationRequestedMessage, bool], Awaitable[None]],
) -> None:
    """Dispatch an investigation message and reschedule unavailable tasks."""

    headers = message.headers or {}
    retry_attempts = headers.get(RETRY_ATTEMPTS_HEADER, 0)

    if type(retry_attempts) is not int or retry_attempts < 0:
        raise ValueError("Invalid retry attempts header")

    final_attempt = retry_attempts >= MAX_INVESTIGATION_RETRIES

    async def dispatch(request: InvestigationRequestedMessage) -> None:
        await processor(request, final_attempt)

    try:
        await handle_investigation_message(
            message=message,
            processor=dispatch,
        )

    except RetryableMessageError as exc:
        if exc.status in ("running", "pending"):
            routing_key = RETRY_ROUTING_KEY
            missing_checks = None

        elif exc.status == "failed":
            routing_key = DEAD_ROUTING_KEY
            missing_checks = None

        elif exc.status is None:
            missing_checks = headers.get(
                MISSING_TASK_CHECKS_HEADER,
                0,
            )

            if type(missing_checks) is not int or missing_checks < 0:
                raise ValueError("Invalid missing task checks header") from exc

            missing_checks += 1

            routing_key = (
                DEAD_ROUTING_KEY
                if missing_checks >= MAX_MISSING_TASK_CHECKS
                else RETRY_ROUTING_KEY
            )

        else:
            raise

        await republish_investigation_message(
            exchange=exchange,
            incoming_message=message,
            routing_key=routing_key,
            retry_attempts=retry_attempts,
            missing_task_checks=missing_checks,
        )

    except RetryableInvestigationError:
        if final_attempt:
            routing_key = DEAD_ROUTING_KEY
            next_attempts = retry_attempts

        else:
            routing_key = RETRY_ROUTING_KEY
            next_attempts = retry_attempts + 1

        await republish_investigation_message(
            exchange=exchange,
            incoming_message=message,
            routing_key=routing_key,
            retry_attempts=next_attempts,
            missing_task_checks=None,
        )
