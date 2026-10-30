"""Request and response schemas for investigation APIs."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, HttpUrl

from maintainer_lens.agents.investigation_analysis import ResponseLanguage
from maintainer_lens.schemas.investigation_budget import KnowledgePreparationBudget
from maintainer_lens.schemas.investigation_contracts import (
    CreateInvestigationRequest as CreateInvestigationRequest,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationResult
from maintainer_lens.schemas.investigation_progress import (
    InvestigationErrorInfo,
    InvestigationProgressEvent,
)
from maintainer_lens.schemas.investigation_reuse import InvestigationFreshness

InvestigationStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "waiting_review",
]


class CreateInvestigationResponse(BaseModel):
    """Response after accepting an investigation request."""

    task_id: str
    status: Literal["pending"]


class InvestigationTaskSummary(BaseModel):
    """An existing task locator; details/events remain separate read endpoints."""

    task_id: str
    response_language: ResponseLanguage = "en"
    status: InvestigationStatus
    generation: int
    created_at: datetime
    updated_at: datetime


class InvestigationLookupPage(BaseModel):
    """Read-only history across preparation modes and budget profiles."""

    repository: str
    issue_number: int
    tasks: list[InvestigationTaskSummary]
    next_before: str | None = None


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
    """Task status, available snapshot and optional completed Graph result."""

    task_id: str
    response_language: ResponseLanguage = "en"
    repository: str
    issue_number: int
    source_index_id: UUID | None = None
    auto_prepare_knowledge: bool = False
    prepared_source_index_id: UUID | None = None
    knowledge_budget: KnowledgePreparationBudget
    status: InvestigationStatus
    issue: IssueSnapshotResponse | None
    result: InvestigationResult | None = None
    report_markdown: str | None = None
    generation: int = 0
    progress: InvestigationProgressEvent | None = None
    last_error: InvestigationErrorInfo | None = None
    freshness: InvestigationFreshness = InvestigationFreshness()
    previous_report_task_id: str | None = None
    previous_report_markdown: str | None = None
