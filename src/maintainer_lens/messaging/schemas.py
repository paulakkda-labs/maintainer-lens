from aio_pika.abc import AbstractIncomingMessage
from pydantic import BaseModel, ConfigDict, Field

RETRY_ATTEMPTS_HEADER = "ml-retry-attempts"

MISSING_TASK_CHECKS_HEADER = "ml-missing-task-checks"


class InvestigationRequestedMessage(BaseModel):
    """Validated payload for an investigation request."""

    task_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    issue_number: int = Field(gt=0, strict=True)

    model_config = ConfigDict(extra="forbid")


def parse_investigation_message(
    message: AbstractIncomingMessage,
) -> InvestigationRequestedMessage:
    """Parse and validate an incoming investigation request."""
    return InvestigationRequestedMessage.model_validate_json(message.body)
