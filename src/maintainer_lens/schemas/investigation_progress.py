"""Safe, persisted investigation progress exposed to API clients."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type InvestigationPhase = Literal[
    "queued",
    "starting",
    "snapshot",
    "preparing_source",
    "preparing_history",
    "preparing_query",
    "searching_history",
    "searching_source",
    "analyzing",
    "planning",
    "tool_search",
    "finalizing",
    "completed",
    "waiting_review",
]
type InvestigationEventKind = Literal[
    "queued",
    "started",
    "progress",
    "retry_scheduled",
    "review_requested",
    "review_approved",
    "review_rejected",
    "failed",
    "completed",
]


class InvestigationErrorInfo(BaseModel):
    """An allowlisted explanation, never raw exception/provider response text."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=256)
    retryable: bool
    not_before: datetime | None = None


class InvestigationProgressEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    event_id: int
    generation: int = Field(ge=0)
    kind: InvestigationEventKind
    phase: InvestigationPhase
    search_round: int | None = Field(default=None, ge=1, le=3)
    error: InvestigationErrorInfo | None = None
    created_at: datetime


class InvestigationEventPage(BaseModel):
    events: list[InvestigationProgressEvent]
    next_after: int | None = None
