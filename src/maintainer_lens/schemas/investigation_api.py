"""Request and response schemas for investigation APIs."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, HttpUrl

from maintainer_lens.schemas.investigation_budget import KnowledgePreparationBudget
from maintainer_lens.schemas.investigation_contracts import (
    CreateInvestigationRequest as CreateInvestigationRequest,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationResult

InvestigationStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
]


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
    """Task status, available snapshot and optional completed Graph result."""

    task_id: str
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
