from typing import Self
from uuid import UUID

from aio_pika.abc import AbstractIncomingMessage
from pydantic import BaseModel, ConfigDict, Field, model_validator

RETRY_ATTEMPTS_HEADER = "ml-retry-attempts"

MISSING_TASK_CHECKS_HEADER = "ml-missing-task-checks"

RETRY_NOT_BEFORE_HEADER = "ml-retry-not-before"

INVESTIGATION_REQUESTED_EVENT_TYPE = "investigation.requested"


class InvestigationRequestedMessage(BaseModel):
    """Validated payload for an investigation request."""

    task_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    issue_number: int = Field(gt=0, strict=True)
    source_index_id: UUID | None = None
    auto_prepare_knowledge: bool = Field(default=False, strict=True)
    generation: int = Field(
        default=0, ge=0, strict=True, exclude_if=lambda value: value == 0
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_preparation_mode(self) -> Self:
        if self.auto_prepare_knowledge and self.source_index_id is not None:
            raise ValueError("Automatic preparation cannot request an explicit index")
        return self


def parse_investigation_message(
    message: AbstractIncomingMessage,
) -> InvestigationRequestedMessage:
    """Parse and validate an incoming investigation request."""
    return InvestigationRequestedMessage.model_validate_json(message.body)
