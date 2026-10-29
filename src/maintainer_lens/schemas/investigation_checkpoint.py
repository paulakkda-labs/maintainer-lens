"""Backend-owned contracts for one durable investigation execution."""

import hashlib
import json
from importlib.metadata import version
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maintainer_lens.agents.investigation.state import HistoryEvidence, SourceEvidence
from maintainer_lens.agents.investigation_analysis import (
    AnalysisEvidence,
    InvestigationAnalysis,
    ResponseLanguage,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationInput

# Analysis context and prompt semantics are part of frozen execution identity.
GRAPH_EXECUTION_VERSION = "investigation-v3"
CHECKPOINT_STORAGE_VERSION = 1


class CheckpointEvidenceContext(InvestigationInput):
    """Validate persisted evidence before resuming; routing may still be pending."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)
    history_evidence: list[HistoryEvidence] = Field(default_factory=list)
    source_evidence: list[SourceEvidence] = Field(default_factory=list)
    analysis_evidence: list[AnalysisEvidence] = Field(default_factory=list)
    preliminary_analysis: InvestigationAnalysis = Field(
        default_factory=lambda: InvestigationAnalysis(
            summary="Analysis has not completed.",
            findings=[],
            hypotheses=[],
            unanswered_questions=[],
        )
    )


class GraphCheckpointConfig(BaseModel):
    """Freeze non-secret execution identity; API keys are deliberately excluded."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    graph_version: str = GRAPH_EXECUTION_VERSION
    storage_version: int = Field(default=CHECKPOINT_STORAGE_VERSION, ge=1, le=1)
    langgraph_version: str = Field(default_factory=lambda: version("langgraph"))
    checkpoint_library_version: str = Field(
        default_factory=lambda: version("langgraph-checkpoint")
    )
    response_language: ResponseLanguage
    max_search_rounds: int = Field(ge=1, le=3)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    search_strategy: Literal["deterministic", "query_planner", "tool"]
    source_enabled: bool
    history_top_k: int = Field(gt=0)
    source_top_k: int = Field(gt=0)
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int = Field(gt=0)
    embedding_endpoint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    llm_model: str
    llm_endpoint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    llm_max_output_tokens: int = Field(gt=0)
    thinking_enabled: bool
    tool_timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @field_validator(
        "graph_version",
        "langgraph_version",
        "checkpoint_library_version",
        "embedding_provider",
        "embedding_model",
        "llm_model",
    )
    @classmethod
    def nonblank_identity(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Execution identity must not be blank")
        return value

    @model_validator(mode="after")
    def validate_tool_budget(self) -> Self:
        if (self.search_strategy == "tool") != (self.tool_timeout_seconds is not None):
            raise ValueError("Tool strategy requires exactly one tool deadline")
        if self.search_strategy != "deterministic" and self.max_search_rounds == 1:
            raise ValueError("Follow-up strategy requires more than one search round")
        return self


class FrozenInvestigationRun(BaseModel):
    """A trusted input/configuration snapshot, not another task status machine."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    task_id: str = Field(min_length=1, max_length=64)
    request: InvestigationInput
    execution: GraphCheckpointConfig
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.request.response_language != self.execution.response_language:
            raise ValueError("Frozen response language mismatch")
        if self.execution.source_enabled != (self.request.source_index_id is not None):
            raise ValueError("Frozen Source identity mismatch")
        if self.fingerprint != execution_fingerprint(self.request, self.execution):
            raise ValueError("Frozen execution fingerprint mismatch")
        return self


def endpoint_fingerprint(base_url: str) -> str:
    """Identify an endpoint without storing its URL or credentials."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("Provider endpoint must not be blank")
    return hashlib.sha256(base_url.rstrip("/").encode()).hexdigest()


def execution_fingerprint(
    request: InvestigationInput, execution: GraphCheckpointConfig
) -> str:
    payload = {
        "request": request.model_dump(mode="json"),
        "execution": execution.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
