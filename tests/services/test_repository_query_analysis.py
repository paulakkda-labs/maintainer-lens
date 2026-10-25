"""Unit tests for repository search query analysis."""

import pytest

from maintainer_lens.services.repository_query_analysis import (
    RepositoryQueryAnalysis,
    analyze_repository_query,
)


@pytest.mark.parametrize(
    ("query", "expected_keyword"),
    [
        (
            "哪里处理 `asyncio.CancelledError`？",
            "asyncio.CancelledError",
        ),
        (
            "Where is `AsyncClient.send()` defined?",
            "AsyncClient.send",
        ),
        (
            "在哪里检查 `user_id`？",
            "user_id",
        ),
        (
            "为什么 `__aenter__` 会报错？",
            "__aenter__",
        ),
        (
            "Where is `send()` implemented?",
            "send",
        ),
    ],
)
def test_extracts_explicit_code_identifiers(
    query: str,
    expected_keyword: str,
) -> None:
    """Prefer code explicitly marked with Markdown backticks."""

    result = analyze_repository_query(query)

    assert isinstance(result, RepositoryQueryAnalysis)
    assert result.query == query
    assert result.keyword == expected_keyword


@pytest.mark.parametrize(
    ("query", "expected_keyword"),
    [
        (
            "哪里处理 asyncio.CancelledError 异常？",
            "asyncio.CancelledError",
        ),
        (
            "Where is AsyncClient.send() defined?",
            "AsyncClient.send",
        ),
        (
            "在哪里检查 user_id？",
            "user_id",
        ),
        (
            "Where is HTTPStatusError handled?",
            "HTTPStatusError",
        ),
        (
            "Where is send() implemented?",
            "send",
        ),
        (
            "How is simple.py used?",
            "simple.py",
        ),
    ],
)
def test_extracts_unquoted_code_identifiers(
    query: str,
    expected_keyword: str,
) -> None:
    """Recognize common code-like tokens in natural-language queries."""

    result = analyze_repository_query(query)

    assert result.keyword == expected_keyword


def test_explicit_code_takes_precedence() -> None:
    """Prefer a backtick-marked identifier over an earlier inferred one."""

    result = analyze_repository_query(
        "HTTPStatusError 是否由 `AsyncClient.send()` 抛出？"
    )

    assert result.keyword == "AsyncClient.send"


def test_chooses_first_unquoted_code_identifier() -> None:
    """Use the first recognized unquoted identifier deterministically."""

    result = analyze_repository_query(
        "Where do CancelledError and HTTPStatusError occur?"
    )

    assert result.keyword == "CancelledError"


@pytest.mark.parametrize(
    "query",
    [
        "哪个函数会把传入的数字加一？",
        "Which function adds one to a given number?",
        "How is request cancellation handled?",
        "请检查 `async request cancellation` 的机制",
        "Why does https://github.com/org/repo/issues/123 fail?",
        "What does ___ mean?",
    ],
)
def test_returns_none_when_no_clear_identifier_exists(
    query: str,
) -> None:
    """Do not invent code keywords from ordinary natural language."""

    result = analyze_repository_query(query)

    assert result.query == query
    assert result.keyword is None


def test_preserves_multilingual_query_and_trims_whitespace() -> None:
    """Keep the original query language without translation."""

    result = analyze_repository_query("  哪里处理 asyncio.CancelledError 异常？  ")

    assert result.query == "哪里处理 asyncio.CancelledError 异常？"
    assert result.keyword == "asyncio.CancelledError"


def test_ignores_oversized_identifier() -> None:
    """Never pass an oversized keyword to literal search."""

    long_identifier = "a" * 129

    result = analyze_repository_query(f"Where is `{long_identifier}` used?")

    assert result.keyword is None


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_rejects_blank_query(query: str) -> None:
    """Reject an empty query before any downstream retrieval."""

    with pytest.raises(ValueError, match="cannot be empty"):
        analyze_repository_query(query)
