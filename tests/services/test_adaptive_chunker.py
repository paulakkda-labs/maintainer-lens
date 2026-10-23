"""Tests for token-budgeted adaptive source chunking."""

import pytest

from maintainer_lens.core.token_count import count_tokens
from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositorySource,
)
from maintainer_lens.services.source_chunker import (
    chunk_repository_source,
)


def make_source(path: str, content: str) -> RepositorySource:
    """Build source metadata for chunking tests."""

    return RepositorySource(
        file=RepositoryFile(
            path=path,
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        content=content,
    )


def test_small_python_file_merges_functions_and_preserves_symbols() -> None:
    """Keep a small file together instead of creating tiny function chunks."""

    source = make_source(
        "src/functions.py",
        "def first():\n    return 1\n\ndef second():\n    return first()\n",
    )

    chunks = chunk_repository_source(source)

    assert len(chunks) == 1
    assert chunks[0].kind == "file"
    assert chunks[0].symbols == ["first", "second"]
    assert chunks[0].content == source.content
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 5)


def test_large_python_file_preserves_all_lines() -> None:
    """Split larger Python files without dropping or duplicating lines."""

    content = "".join(f"def function_{i}():\n    return {i}\n\n" for i in range(20))
    source = make_source("src/functions.py", content)

    # Treat each character as one unit to make the test deterministic.
    chunks = chunk_repository_source(
        source,
        max_tokens=90,
        token_counter=len,
    )

    assert len(chunks) > 1

    # Python uses no overlap; concatenation must recover the exact file.
    assert "".join(chunk.content for chunk in chunks) == content

    assert all(len(chunk.content) <= 90 for chunk in chunks)

    symbols = {symbol for chunk in chunks for symbol in chunk.symbols}
    assert symbols == {f"function_{i}" for i in range(20)}

    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == len(content.splitlines())


def test_oversized_python_function_falls_back_to_lines() -> None:
    """Do not exceed the budget for a single oversized function."""

    content = "def large_function():\n" + "    value += 1\n" * 30
    source = make_source("src/large.py", content)

    chunks = chunk_repository_source(
        source,
        max_tokens=65,
        token_counter=len,
    )

    assert len(chunks) > 1
    assert "".join(chunk.content for chunk in chunks) == content
    assert all(len(chunk.content) <= 65 for chunk in chunks)


def test_javascript_fallback_preserves_coverage_and_overlap() -> None:
    """Use overlapping bounded windows without losing source lines."""

    content = "".join(f"const value{i} = {i};\n" for i in range(30))
    source = make_source("src/index.js", content)

    chunks = chunk_repository_source(
        source,
        max_tokens=100,
        token_counter=len,
    )

    assert len(chunks) > 1
    assert all(len(chunk.content) <= 100 for chunk in chunks)

    total_lines = len(content.splitlines())
    covered_lines: set[int] = set()

    for chunk in chunks:
        covered_lines.update(range(chunk.start_line, chunk.end_line + 1))

    assert covered_lines == set(range(1, total_lines + 1))

    # Adjacent text windows intentionally share some source lines.
    assert any(
        right.start_line <= left.end_line
        for left, right in zip(chunks, chunks[1:], strict=False)
    )


def test_token_budget_can_be_stricter_than_line_budget() -> None:
    """Respect token limits even for files with very few lines."""

    source = make_source(
        "README.md",
        "one two three four five\nsix seven eight nine ten\n",
    )

    def word_counter(text: str) -> int:
        return len(text.split())

    chunks = chunk_repository_source(
        source,
        max_tokens=5,
        token_counter=word_counter,
    )

    assert len(chunks) == 2
    assert all(word_counter(chunk.content) <= 5 for chunk in chunks)


def test_tokenizer_accepts_literal_special_token_text() -> None:
    """Source code may contain strings resembling model special tokens."""

    assert count_tokens("<|endoftext|>") >= 1


def test_reject_invalid_token_budget() -> None:
    """Reject unusable chunk budget configuration."""

    source = make_source("README.md", "hello\n")

    with pytest.raises(ValueError, match="max_tokens"):
        chunk_repository_source(source, max_tokens=0)
