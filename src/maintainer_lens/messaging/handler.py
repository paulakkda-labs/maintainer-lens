"""Message handling for investigation requests."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from aio_pika.abc import AbstractIncomingMessage, AbstractRobustExchange
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_tasks import (
    get_investigation_task_generation,
    get_investigation_task_status,
)
from maintainer_lens.investigation_runner import (
    InvestigationClaimUnavailableError,
    LeaseLostError,
    PermanentInvestigationError,
    RetryableInvestigationError,
    execute_investigation_task,
)
from maintainer_lens.messaging.publisher import republish_investigation_message
from maintainer_lens.messaging.schemas import (
    MISSING_TASK_CHECKS_HEADER,
    RETRY_ATTEMPTS_HEADER,
    RETRY_NOT_BEFORE_HEADER,
    InvestigationRequestedMessage,
    parse_investigation_message,
)
from maintainer_lens.messaging.topology import DEAD_ROUTING_KEY, RETRY_ROUTING_KEY

MAX_MISSING_TASK_CHECKS = 3

MAX_INVESTIGATION_RETRIES = 3


class InvalidInvestigationMessageError(ValueError):
    """The delivery body cannot represent an investigation request."""


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
    processor: Callable[[InvestigationRequestedMessage], Awaitable[None]],
) -> None:
    """Validate and dispatch an investigation request for processing."""
    try:
        request = parse_investigation_message(message)
    except ValidationError as exc:
        raise InvalidInvestigationMessageError("Invalid investigation message") from exc
    await processor(request)


async def process_investigation_request(
    request: InvestigationRequestedMessage,
    session_factory: async_sessionmaker[AsyncSession],
    processor: Callable[[str], Awaitable[None]],
    final_attempt: bool = False,
    event_recorder=None,
) -> None:
    """Execute an investigation or determine whether its message needs retrying."""
    try:
        await execute_investigation_task(
            session_factory=session_factory,
            task_id=request.task_id,
            processor=processor,
            final_attempt=final_attempt,
            **(
                {"expected_generation": request.generation}
                if request.generation
                else {}
            ),
            **(
                {"event_recorder": event_recorder} if event_recorder is not None else {}
            ),
        )

    except (InvestigationClaimUnavailableError, LeaseLostError, ExceptionGroup) as exc:
        if isinstance(exc, ExceptionGroup):
            _, remaining = exc.split(LeaseLostError)
            if remaining is not None:
                raise
        async with session_factory() as session:
            current_generation = await get_investigation_task_generation(
                session, request.task_id
            )
            if current_generation is not None:
                if current_generation > request.generation:
                    return  # A late delivery must never restart a newer generation.
                if current_generation < request.generation:
                    raise PermanentInvestigationError(
                        "Message generation is ahead of its task"
                    ) from exc
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


def parse_retry_not_before(headers: dict) -> datetime | None:
    """Parse the earliest permitted retry time from message headers."""

    raw = headers.get(RETRY_NOT_BEFORE_HEADER)

    if raw is None:
        return None

    if not isinstance(raw, str):
        raise ValueError("Invalid retry deadline header")

    try:
        deadline = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Invalid retry deadline header") from exc

    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("Retry deadline must include a timezone")

    return deadline.astimezone(UTC)


async def handle_investigation_message_with_retry(
    message: AbstractIncomingMessage,
    exchange: AbstractRobustExchange,
    processor: Callable[[InvestigationRequestedMessage, bool], Awaitable[None]],
) -> None:
    """Dispatch investigation messages with retry and deadline handling."""

    headers = message.headers or {}
    retry_attempts = headers.get(RETRY_ATTEMPTS_HEADER, 0)

    try:
        if type(retry_attempts) is not int or retry_attempts < 0:
            raise ValueError("Invalid retry attempts header")
        missing_checks = headers.get(MISSING_TASK_CHECKS_HEADER, 0)
        if type(missing_checks) is not int or missing_checks < 0:
            raise ValueError("Invalid missing task checks header")
        deadline = parse_retry_not_before(headers)
    except ValueError:
        await republish_investigation_message(
            exchange=exchange,
            incoming_message=message,
            routing_key=DEAD_ROUTING_KEY,
            retry_attempts=0,
        )
        return

    if deadline is not None:
        if datetime.now(UTC) < deadline:
            await republish_investigation_message(
                exchange=exchange,
                incoming_message=message,
                routing_key=RETRY_ROUTING_KEY,
                retry_attempts=retry_attempts,
                retry_not_before=deadline.isoformat(),
            )
            return

    final_attempt = retry_attempts >= MAX_INVESTIGATION_RETRIES

    async def dispatch(request: InvestigationRequestedMessage) -> None:
        await processor(request, final_attempt)

    try:
        await handle_investigation_message(
            message=message,
            processor=dispatch,
        )

    except InvalidInvestigationMessageError:
        await republish_investigation_message(
            exchange=exchange,
            incoming_message=message,
            routing_key=DEAD_ROUTING_KEY,
            retry_attempts=retry_attempts,
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

    except RetryableInvestigationError as exc:
        if final_attempt:
            routing_key = DEAD_ROUTING_KEY
            next_attempts = retry_attempts
        else:
            routing_key = RETRY_ROUTING_KEY
            next_attempts = retry_attempts + 1

        retry_options: dict[str, str] = {}

        if routing_key == RETRY_ROUTING_KEY and exc.not_before is not None:
            deadline = exc.not_before

            if deadline.tzinfo is None or deadline.utcoffset() is None:
                raise ValueError("Retry deadline must include a timezone") from exc

            retry_options["retry_not_before"] = deadline.astimezone(UTC).isoformat()

        await republish_investigation_message(
            exchange=exchange,
            incoming_message=message,
            routing_key=routing_key,
            retry_attempts=next_attempts,
            missing_task_checks=None,
            **retry_options,
        )

    except PermanentInvestigationError:
        await republish_investigation_message(
            exchange=exchange,
            incoming_message=message,
            routing_key=DEAD_ROUTING_KEY,
            retry_attempts=retry_attempts,
        )
