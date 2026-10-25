from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvidenceAnchor(BaseModel):
    """Identify relevant source evidence independently of chunk keys."""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    required_text: str = Field(min_length=1)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        path = PurePosixPath(value)

        if (
            not value.strip()
            or value != value.strip()
            or path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or "\\" in value
            or str(path) != value
        ):
            raise ValueError("file_path must be a normalized relative POSIX path")

        return value

    @field_validator("required_text")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required_text must not be blank")

        return value

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.start_line > self.end_line:
            raise ValueError("start_line must not exceed end_line")

        return self


class RetrievalCase(BaseModel):
    """Represent a manually labeled retrieval question."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    language: Literal["en", "zh"]
    query: str = Field(min_length=1)
    relevant_evidence: list[EvidenceAnchor] = Field(min_length=1)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")

        return value

    @model_validator(mode="after")
    def validate_unique_evidence(self) -> Self:
        anchors = [
            (
                anchor.file_path,
                anchor.start_line,
                anchor.end_line,
                anchor.required_text,
            )
            for anchor in self.relevant_evidence
        ]

        if len(anchors) != len(set(anchors)):
            raise ValueError("relevant_evidence contains duplicate anchors")

        return self


class RetrievalDataset(BaseModel):
    """Store retrieval ground truth for a fixed repository revision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    cases: list[RetrievalCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_case_ids(self) -> Self:
        ids = [case.id for case in self.cases]

        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")

        return self


def load_retrieval_dataset(path: Path) -> RetrievalDataset:
    """Read and validate a JSON retrieval evaluation dataset."""

    return RetrievalDataset.model_validate_json(path.read_text(encoding="utf-8"))
