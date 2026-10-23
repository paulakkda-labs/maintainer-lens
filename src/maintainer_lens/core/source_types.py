"""Identify source formats independently of parsing implementations."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

SourceCategory = Literal["code", "document", "config"]


@dataclass(frozen=True)
class SourceType:
    """A recognized source format."""

    language: str
    category: SourceCategory


_SUFFIX_TYPES = {
    ".py": SourceType("python", "code"),
    ".pyi": SourceType("python", "code"),
    ".js": SourceType("javascript", "code"),
    ".jsx": SourceType("javascript", "code"),
    ".ts": SourceType("typescript", "code"),
    ".tsx": SourceType("typescript", "code"),
    ".go": SourceType("go", "code"),
    ".java": SourceType("java", "code"),
    ".rs": SourceType("rust", "code"),
    ".c": SourceType("c", "code"),
    ".h": SourceType("c", "code"),
    ".cpp": SourceType("cpp", "code"),
    ".hpp": SourceType("cpp", "code"),
    ".cs": SourceType("csharp", "code"),
    ".rb": SourceType("ruby", "code"),
    ".sh": SourceType("shell", "code"),
    ".md": SourceType("markdown", "document"),
    ".mdx": SourceType("mdx", "document"),
    ".rst": SourceType("rst", "document"),
    ".txt": SourceType("text", "document"),
    ".json": SourceType("json", "config"),
    ".yaml": SourceType("yaml", "config"),
    ".yml": SourceType("yaml", "config"),
    ".toml": SourceType("toml", "config"),
    ".xml": SourceType("xml", "config"),
    ".sql": SourceType("sql", "config"),
    ".html": SourceType("html", "code"),
    ".css": SourceType("css", "code"),
    ".tf": SourceType("terraform", "config"),
    ".properties": SourceType("properties", "config"),
}

_SPECIAL_FILENAMES = {
    "dockerfile": SourceType("dockerfile", "config"),
    "makefile": SourceType("makefile", "config"),
    "license": SourceType("text", "document"),
    ".gitignore": SourceType("gitignore", "config"),
    ".dockerignore": SourceType("dockerignore", "config"),
    ".env.example": SourceType("dotenv", "config"),
    ".env.sample": SourceType("dotenv", "config"),
}

_LOCK_FILES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "cargo.lock",
    }
)


def is_excluded_source(path: str) -> bool:
    """Check whether a filename is explicitly excluded by policy."""

    filename = PurePosixPath(path).name.lower()

    if filename == ".env":
        return True

    if filename.startswith(".env.") and filename not in {
        ".env.example",
        ".env.sample",
    }:
        return True

    return filename in _LOCK_FILES


def detect_source_type(path: str) -> SourceType | None:
    """Recognize supported text formats using filenames and suffixes."""

    filename = PurePosixPath(path).name.lower()

    # Never index environment secrets as ordinary source files.
    if is_excluded_source(path):
        return None

    if filename.startswith("dockerfile."):
        return SourceType("dockerfile", "config")

    if filename in _SPECIAL_FILENAMES:
        return _SPECIAL_FILENAMES[filename]

    return _SUFFIX_TYPES.get(PurePosixPath(filename).suffix)
