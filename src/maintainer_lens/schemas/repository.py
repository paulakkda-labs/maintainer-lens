"""Data models for versioned GitHub repositories."""

from typing import Literal

from pydantic import BaseModel, Field


class RepositoryRevision(BaseModel):
    """An immutable Git repository revision resolved from a branch."""

    repository: str = Field(min_length=1)
    default_branch: str = Field(min_length=1)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class RepositoryFile(BaseModel):
    """An indexable regular file from a Git tree."""

    path: str = Field(min_length=1)
    blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    size: int = Field(ge=0)
    mode: Literal["100644", "100755"]


class RepositorySkippedPath(BaseModel):
    """A repository path skipped by the manifest selection policy."""

    path: str = Field(min_length=1)
    reason: Literal[
        "excluded_directory",
        "excluded_file",
        "unsupported_format",
        "file_too_large",
        "non_regular_file",
    ]


class RepositoryManifest(BaseModel):
    """Indexable files associated with a fixed repository revision."""

    repository: str
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: list[RepositoryFile]
    skipped_paths: list[RepositorySkippedPath] = Field(default_factory=list)


class RepositorySource(BaseModel):
    """Verified source content associated with a Git tree entry."""

    file: RepositoryFile
    content: str
