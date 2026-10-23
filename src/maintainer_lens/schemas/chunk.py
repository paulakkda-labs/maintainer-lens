"""Data models for language-aware source chunks."""

from typing import Literal

from pydantic import BaseModel, Field

from maintainer_lens.schemas.repository import RepositoryFile


class CodeChunk(BaseModel):
    """A source segment with language and location metadata."""

    file: RepositoryFile
    language: str = Field(min_length=1)
    kind: Literal[
        "file",
        "text",
        "section",
        "module",
        "function",
        "async_function",
        "class",
        "method",
        "async_method",
        "interface",
        "type_alias",
    ]
    symbol: str | None = None
    symbols: list[str] = Field(default_factory=list)
    heading_path: list[str] = Field(default_factory=list)
    headings: list[str] = Field(default_factory=list)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    content: str = Field(min_length=1)
