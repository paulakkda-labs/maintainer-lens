"""Versioned human decisions over trusted investigation progress and soft budgets."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from maintainer_lens.schemas.investigation_budget import (
    KnowledgeBudgetProfile,
    KnowledgePreparationBudget,
    list_knowledge_budget_profiles,
)
from maintainer_lens.schemas.investigation_checkpoint import GraphCheckpointConfig
from maintainer_lens.schemas.investigation_contracts import InvestigationResult

MAX_BUDGET_RESUMES = 3
HUMAN_ACCEPTANCE_REASON = (
    "Human accepted the last complete analysis; later investigation was unfinished."
)
type ReviewReason = Literal[
    "source_files",
    "source_bytes",
    "source_chunks",
    "preparation_timeout_seconds",
    "analysis_generation_limit",
]


class ReviewContext(BaseModel):
    """An immutable request snapshot; provider text and credentials are excluded."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: int = Field(default=1, ge=1, le=1)
    reason: ReviewReason
    observed: int | None = Field(default=None, ge=0)
    knowledge_budget: KnowledgePreparationBudget
    base_execution: GraphCheckpointConfig
    output_tokens: int = Field(gt=0)
    run_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=64)
    search_round: int | None = Field(default=None, ge=1, le=3)
    partial_result: InvestigationResult | None = None
    partial_no_new_evidence: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if (self.run_fingerprint is None) != (self.checkpoint_id is None):
            raise ValueError(
                "Run and checkpoint review identities must be recorded together"
            )
        if (
            self.search_round is not None
            and self.search_round > self.base_execution.max_search_rounds
        ):
            raise ValueError("Review search round exceeds the frozen budget")
        if self.output_tokens < self.base_execution.llm_max_output_tokens:
            raise ValueError("Approved output budget cannot lower the base budget")
        if self.partial_result is not None and (
            self.run_fingerprint is None or self.checkpoint_id is None
        ):
            raise ValueError("A partial analysis requires a frozen checkpoint")
        if (
            self.reason != "analysis_generation_limit"
            and self.partial_result is not None
        ):
            raise ValueError("Preparation has no acceptable Graph analysis")
        return self


class ReviewDecision(BaseModel):
    """Idempotency is scoped to the addressed review request."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    decision_id: UUID = Field(strict=False)
    action: Literal["increase_budget", "accept_partial", "reject"]
    knowledge_budget_profile: KnowledgeBudgetProfile | None = None
    max_output_tokens: int | None = Field(default=None, gt=0)
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        count = (self.knowledge_budget_profile is not None) + (
            self.max_output_tokens is not None
        )
        if (self.action == "increase_budget" and count != 1) or (
            self.action != "increase_budget" and count
        ):
            raise ValueError(
                "Only a budget decision accepts exactly one budget selection"
            )
        return self


class ReviewResponse(BaseModel):
    review_id: UUID
    task_id: str
    generation: int
    created_at: datetime
    context: ReviewContext
    decision: ReviewDecision | None
    decided_at: datetime | None
    target_generation: int | None
    can_increase_budget: bool
    allowed_knowledge_profiles: list[KnowledgeBudgetProfile]
    can_accept_partial: bool
    output_ceiling: int | None
    budget_resumes_used: int
    user_recoveries_used: int


def allowed_preparation_profiles(
    context: ReviewContext,
) -> list[KnowledgeBudgetProfile]:
    if context.reason == "analysis_generation_limit":
        return []
    field = {
        "source_files": "source_max_files",
        "source_bytes": "source_max_bytes",
        "source_chunks": "source_max_chunks",
        "preparation_timeout_seconds": "preparation_timeout_seconds",
    }[context.reason]
    fields = (
        "source_max_files",
        "source_max_bytes",
        "source_max_chunks",
        "history_max_items",
        "preparation_timeout_seconds",
    )
    current = context.knowledge_budget
    allowed = []
    for budget in list_knowledge_budget_profiles():
        if any(
            (getattr(current, key) is None and getattr(budget, key) is not None)
            or (
                getattr(current, key) is not None
                and getattr(budget, key) is not None
                and getattr(budget, key) < getattr(current, key)
            )
            for key in fields
        ):
            continue
        before, after = getattr(current, field), getattr(budget, field)
        if before is None or (after is not None and after <= before):
            continue
        if (
            context.observed is not None
            and after is not None
            and after < context.observed
        ):
            continue
        allowed.append(budget.profile)
    return allowed
