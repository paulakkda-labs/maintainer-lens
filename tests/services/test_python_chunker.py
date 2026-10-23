"""Tests for Python AST-aware source chunking."""

import pytest

from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositorySource,
)
from maintainer_lens.services.python_chunker import chunk_python_source


def make_source(
    content: str,
    path: str = "src/sample.py",
) -> RepositorySource:
    """Create an in-memory source model for AST tests."""

    return RepositorySource(
        file=RepositoryFile(
            path=path,
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        content=content,
    )


def test_chunk_python_source_preserves_definitions_and_locations() -> None:
    """Split top-level definitions without losing module-level code."""

    source = make_source(
        "import os\n"
        "\n"
        "@trace\n"
        "async def fetch():\n"
        "    return os.name\n"
        "\n"
        "class Service:\n"
        "    def run(self):\n"
        "        return 1\n"
        "\n"
        "VERSION = 1\n"
    )

    chunks = chunk_python_source(source)

    assert [
        (chunk.kind, chunk.symbol, chunk.start_line, chunk.end_line) for chunk in chunks
    ] == [
        ("module", None, 1, 1),
        ("async_function", "fetch", 3, 5),
        ("class", "Service", 7, 9),
        ("module", None, 11, 11),
    ]

    assert chunks[0].content == "import os\n"
    assert chunks[1].content.startswith("@trace\nasync def fetch():")
    assert "def run(self):" in chunks[2].content
    assert chunks[3].content == "VERSION = 1\n"

    assert all(chunk.file == source.file for chunk in chunks)


def test_chunk_python_source_keeps_nested_functions_together() -> None:
    """Keep nested definitions within their containing function."""

    source = make_source(
        "def outer():\n    def inner():\n        return 1\n    return inner()\n"
    )

    chunks = chunk_python_source(source)

    assert len(chunks) == 1
    assert chunks[0].kind == "function"
    assert chunks[0].symbol == "outer"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 4
    assert "def inner():" in chunks[0].content


def test_chunk_python_source_keeps_multiline_decorator() -> None:
    """Include all decorator lines in the function chunk."""

    source = make_source(
        "@decorate(\n    enabled=True,\n)\ndef work():\n    return 1\n"
    )

    chunks = chunk_python_source(source)

    assert len(chunks) == 1
    assert chunks[0].kind == "function"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 5
    assert chunks[0].content == source.content


def test_chunk_python_source_handles_module_only() -> None:
    """Preserve a module containing no function or class definitions."""

    source = make_source("# Module configuration\nimport os\n\nANSWER = 42\n")

    chunks = chunk_python_source(source)

    assert len(chunks) == 1
    assert chunks[0].kind == "module"
    assert chunks[0].content == source.content


def test_chunk_python_source_rejects_invalid_syntax() -> None:
    """Do not silently index syntactically invalid Python source."""

    source = make_source("def broken(:\n    pass\n")

    with pytest.raises(SyntaxError):
        chunk_python_source(source)


def test_chunk_python_source_rejects_non_python_file() -> None:
    """Reject documents that require a different chunking strategy."""

    source = make_source("# README", path="README.md")

    with pytest.raises(ValueError, match="Not a Python source file"):
        chunk_python_source(source)


def test_chunk_python_source_handles_empty_file() -> None:
    """An empty source file produces no chunks."""

    assert chunk_python_source(make_source(" \n\n")) == []
