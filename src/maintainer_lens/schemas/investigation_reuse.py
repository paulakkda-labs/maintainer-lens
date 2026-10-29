"""URL resolution and explicitly scoped report freshness contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type FreshnessStatus = Literal["not_checked", "checking", "current", "stale", "unknown"]


class InvestigationFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    status: FreshnessStatus = "not_checked"
    scope: Literal["issue-and-default-branch"] = "issue-and-default-branch"
    checked_at: datetime | None = None
    observed_issue_state: Literal["open", "closed"] | None = None
    observed_commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    report_commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    reason_code: str | None = Field(default=None, max_length=64)
    message: str | None = Field(default=None, max_length=256)


class ResolveInvestigationResponse(BaseModel):
    """The selected task and, when available, an explicitly identified old report."""

    task_id: str
    status: Literal["pending", "running", "succeeded", "failed", "waiting_review"]
    decision: Literal[
        "created",
        "active",
        "reused",
        "resumed",
        "blocked",
        "checking",
        "unverified",
        "awaiting_review",
    ]
    generation: int = Field(ge=0)
    previous_task_id: str | None = None
    report_task_id: str | None = None
    report_markdown: str | None = None
    freshness: InvestigationFreshness
