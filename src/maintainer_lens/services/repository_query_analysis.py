"""Analyze repository search queries and extract code identifiers."""

import re
from dataclasses import dataclass

MAX_KEYWORD_LENGTH = 128

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_QUALIFIED_IDENTIFIER = rf"{_IDENTIFIER}(?:\.{_IDENTIFIER})*"

_BACKTICK_IDENTIFIER_RE = re.compile(
    rf"(?<!`)`(?P<identifier>{_QUALIFIED_IDENTIFIER})(?:\(\))?`(?!`)"
)

_INLINE_IDENTIFIER_RE = re.compile(
    rf"(?<![A-Za-z0-9_./])"
    rf"(?P<identifier>{_QUALIFIED_IDENTIFIER})"
    rf"(?P<call>\(\))?"
    rf"(?![A-Za-z0-9_./])"
)

_CAMEL_CASE_RE = re.compile(r"[a-z][A-Z]")


@dataclass(frozen=True, slots=True)
class RepositoryQueryAnalysis:
    """A normalized query and an optional literal code keyword."""

    query: str
    keyword: str | None


def _looks_like_code_identifier(
    identifier: str,
    *,
    is_call: bool,
) -> bool:
    """Recognize common code-like identifier shapes."""

    if not any(char.isalnum() for char in identifier):
        return False

    return (
        is_call
        or "." in identifier
        or "_" in identifier
        or _CAMEL_CASE_RE.search(identifier) is not None
    )


def analyze_repository_query(query: str) -> RepositoryQueryAnalysis:
    """Extract a high-confidence code identifier without translation."""

    normalized_query = query.strip()

    if not normalized_query:
        raise ValueError("Search query cannot be empty")

    # Explicitly marked code takes precedence over inferred identifiers.
    for match in _BACKTICK_IDENTIFIER_RE.finditer(normalized_query):
        identifier = match.group("identifier")

        if len(identifier) <= MAX_KEYWORD_LENGTH:
            return RepositoryQueryAnalysis(
                query=normalized_query,
                keyword=identifier,
            )

    # Look for code-like tokens in ordinary unquoted text.
    for match in _INLINE_IDENTIFIER_RE.finditer(normalized_query):
        identifier = match.group("identifier")

        if len(identifier) > MAX_KEYWORD_LENGTH:
            continue

        is_call = match.group("call") is not None

        if _looks_like_code_identifier(
            identifier,
            is_call=is_call,
        ):
            return RepositoryQueryAnalysis(
                query=normalized_query,
                keyword=identifier,
            )

    # No reliable code identifier was detected.
    return RepositoryQueryAnalysis(
        query=normalized_query,
        keyword=None,
    )
