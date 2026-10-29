"""Describe report evidence coverage without claiming semantic correctness."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from maintainer_lens.schemas.investigation_contracts import (
    InvestigationIdentity,
    InvestigationResult,
)
from maintainer_lens.services.investigation_report import render_investigation_report


class ReportReviewCase(InvestigationIdentity):
    """A human review prompt, not a gold answer or a frozen GitHub snapshot."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    observed_issue_state: Literal["open", "closed"]
    review_focus: list[str] = Field(min_length=1)
    unsupported_claims: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_review_scope(self) -> Self:
        if self.source_index_id is not None:
            raise ValueError("Review cases use URL intake, not a prepared Source UUID")
        if any(
            not item.strip() for item in [*self.review_focus, *self.unsupported_claims]
        ):
            raise ValueError("Review instructions must not be blank")
        return self

    @property
    def issue_url(self) -> str:
        return f"https://github.com/{self.repository}/issues/{self.issue_number}"


class ReportReviewDataset(BaseModel):
    """A small curated review set; live inputs and conclusions can change."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    observed_on: date
    cases: list[ReportReviewCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_cases(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Review case IDs must be unique")
        identities = {
            (case.repository.casefold(), case.issue_number) for case in self.cases
        }
        if len(identities) != len(self.cases):
            raise ValueError("Review Issue identities must be unique")
        return self


def load_report_review_dataset(path: Path) -> ReportReviewDataset:
    """Load review prompts without clients, settings or external requests."""
    return ReportReviewDataset.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class ReportEvidenceObservations:
    """Structural observations only; none is a semantic accuracy score."""

    next_step: str
    search_round: int
    finding_count: int
    distinct_cited_evidence: int
    retrieved_history: int
    retrieved_source: int
    analyzed_history: int
    analyzed_source: int
    cited_history: int
    cited_source: int
    hypothesis_count: int
    unanswered_question_count: int


def inspect_report_evidence(result: InvestigationResult) -> ReportEvidenceObservations:
    """Reuse report/provenance validation and count the actual saved context.

    Never reconstruct analysis input from final evidence ranks. A complete
    analysis can coexist with subsequently retrieved but unanalyzed evidence.
    Counts do not measure relevance, entailment, root-cause accuracy or costs.
    """
    result = InvestigationResult.model_validate(result.model_dump(), strict=True)
    render_investigation_report(result)
    context = {item["evidence_id"]: item for item in result.analysis_evidence}
    cited_ids = {
        evidence_id
        for finding in result.preliminary_analysis.findings
        for evidence_id in finding.evidence_ids
    }
    return ReportEvidenceObservations(
        next_step=result.next_step,
        search_round=result.search_round,
        finding_count=len(result.preliminary_analysis.findings),
        distinct_cited_evidence=len(cited_ids),
        retrieved_history=len(result.history_evidence),
        retrieved_source=len(result.source_evidence),
        analyzed_history=sum(
            item["category"] == "history" for item in context.values()
        ),
        analyzed_source=sum(item["category"] == "source" for item in context.values()),
        cited_history=sum(context[item]["category"] == "history" for item in cited_ids),
        cited_source=sum(context[item]["category"] == "source" for item in cited_ids),
        hypothesis_count=len(result.preliminary_analysis.hypotheses),
        unanswered_question_count=len(result.preliminary_analysis.unanswered_questions),
    )
