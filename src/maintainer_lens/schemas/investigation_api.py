"""Request and response schemas for investigation APIs."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

InvestigationStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
]


class CreateInvestigationRequest(BaseModel):
    """Request to investigate a public GitHub issue."""

    repository: str
    issue_number: int = Field(gt=0)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        """Validate an owner/repository identifier."""

        parts = value.split("/")

        if len(parts) != 2 or any(
            part in (".", "..") or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
            for part in parts
        ):
            raise ValueError("Repository must use the format owner/repository")

        return value


class CreateInvestigationResponse(BaseModel):
    """Response after accepting an investigation request."""

    task_id: str
    status: Literal["pending"]


class IssueSnapshotResponse(BaseModel):
    """GitHub issue snapshot stored by the investigator."""

    title: str
    body: str | None
    state: Literal["open", "closed"]
    source_url: HttpUrl
    source_created_at: datetime
    source_updated_at: datetime
    fetched_at: datetime


class InvestigationDetailResponse(BaseModel):
    """Current investigation status and available issue snapshot."""

    task_id: str
    repository: str
    issue_number: int
    status: InvestigationStatus
    issue: IssueSnapshotResponse | None
