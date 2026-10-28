"""Versioned knowledge-preparation budgets, independent of Graph execution."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type KnowledgeBudgetProfile = Literal["small", "standard", "large", "unlimited"]


class KnowledgePreparationBudget(BaseModel):
    """A submission-time snapshot; None means no task-level preparation limit.

    Persist these values rather than resolving the profile again on retries.
    Graph rounds/deadlines and transport/content safety controls are separate.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: int = Field(ge=1, le=1)
    profile: KnowledgeBudgetProfile
    profile_version: int = Field(gt=0)
    source_max_files: int | None = Field(gt=0)
    source_max_bytes: int | None = Field(gt=0)
    source_max_chunks: int | None = Field(gt=0)
    history_max_items: int | None = Field(gt=0)
    preparation_timeout_seconds: int | None = Field(gt=0)

    @model_validator(mode="after")
    def validate_profile_limits(self) -> Self:
        limits = (
            self.source_max_files,
            self.source_max_bytes,
            self.source_max_chunks,
            self.history_max_items,
            self.preparation_timeout_seconds,
        )
        if self.profile == "unlimited":
            if any(value is not None for value in limits):
                raise ValueError("Unlimited preparation requires null task limits")
        elif any(value is None for value in limits):
            raise ValueError("Bounded preparation profiles require positive limits")
        return self


def make_knowledge_preparation_budget(
    profile: KnowledgeBudgetProfile = "standard",
) -> KnowledgePreparationBudget:
    """Resolve an allowed profile once, without settings, clients or I/O."""
    limits = {
        "small": (20, 256 * 1024, 100, 1, 300),
        "standard": (500, 8 * 1024 * 1024, 2000, 3, 900),
        "large": (2000, 8 * 1024 * 1024, 4000, 5, 1800),
        "unlimited": (None, None, None, None, None),
    }
    if not isinstance(profile, str) or profile not in limits:
        raise ValueError("Unsupported knowledge budget profile")
    files, source_bytes, chunks, history_items, seconds = limits[profile]
    return KnowledgePreparationBudget(
        schema_version=1,
        profile=profile,
        profile_version=1,
        source_max_files=files,
        source_max_bytes=source_bytes,
        source_max_chunks=chunks,
        history_max_items=history_items,
        preparation_timeout_seconds=seconds,
    )


def list_knowledge_budget_profiles() -> list[KnowledgePreparationBudget]:
    """Describe current choices; stored task snapshots remain independent."""
    return [
        make_knowledge_preparation_budget("small"),
        make_knowledge_preparation_budget("standard"),
        make_knowledge_preparation_budget("large"),
        make_knowledge_preparation_budget("unlimited"),
    ]
