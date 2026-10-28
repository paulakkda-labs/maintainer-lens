"""Shared request and result models for HTTP and application callers."""

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from maintainer_lens.agents.investigation.state import (
    HistoryEvidence,
    InvestigationNextStep,
    SourceEvidence,
)
from maintainer_lens.agents.investigation_analysis import (
    AnalysisEvidence,
    InvestigationAnalysis,
    ResponseLanguage,
)
from maintainer_lens.schemas.investigation_budget import KnowledgeBudgetProfile


class InvestigationIdentity(BaseModel):
    """Shared repository/Issue/explicit-index identity without execution options."""

    repository: str
    issue_number: int = Field(gt=0)
    source_index_id: UUID | None = Field(default=None, strict=False)

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


class CreateInvestigationRequest(InvestigationIdentity):
    """HTTP submission options; the backend snapshots the selected profile."""

    knowledge_budget_profile: KnowledgeBudgetProfile = "standard"


class InvestigationInput(InvestigationIdentity):
    """Caller-owned issue identity and context, without graph control fields."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    issue_title: str
    issue_body: str | None
    response_language: ResponseLanguage

    @field_validator("issue_title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Issue title must not be blank")
        return value


class InvestigationResult(InvestigationInput):
    """Validated application output; internal graph controls are not exported."""

    next_step: InvestigationNextStep
    preliminary_analysis: InvestigationAnalysis
    history_evidence: list[HistoryEvidence]
    source_evidence: list[SourceEvidence]
    analysis_evidence: list[AnalysisEvidence]
    search_round: int = Field(ge=1, le=3)
    routing_reason: str = Field(min_length=1)
