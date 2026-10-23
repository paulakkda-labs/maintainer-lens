"""Tests for Go Tree-sitter structural analysis."""

import pytest

from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositorySource,
)
from maintainer_lens.services.go_structure import analyze_go_source
from maintainer_lens.services.source_chunker import chunk_repository_source


def make_source(content: str) -> RepositorySource:
    """Create an in-memory Go source file."""

    return RepositorySource(
        file=RepositoryFile(
            path="src/service.go",
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        content=content,
    )


def test_go_functions_and_receiver_methods() -> None:
    """Recognize functions, pointer receivers, and value receivers."""

    source = make_source(
        "package service\n"
        "\n"
        "type Worker struct{}\n"
        "\n"
        "func Add(a, b int) int {\n"
        "    return a + b\n"
        "}\n"
        "\n"
        "func (w *Worker) Run() int {\n"
        "    return Add(1, 2)\n"
        "}\n"
        "\n"
        "func (w Worker) Stop() {}\n"
    )

    structures = analyze_go_source(source)

    assert [(item.kind, item.symbol) for item in structures] == [
        ("function", "Add"),
        ("method", "Worker.Run"),
        ("method", "Worker.Stop"),
    ]

    assert (structures[0].start_line, structures[0].end_line) == (
        5,
        7,
    )
    assert structures[1].content.startswith("func (w *Worker) Run()")


def test_go_generic_receiver() -> None:
    """Recognize a generic receiver's base type."""

    source = make_source(
        "package cache\n"
        "\n"
        "type Cache[T any] struct{}\n"
        "\n"
        "func (c *Cache[T]) Get() T {\n"
        "    var zero T\n"
        "    return zero\n"
        "}\n"
    )

    structures = analyze_go_source(source)

    assert len(structures) == 1
    assert structures[0].symbol == "Cache.Get"


def test_go_adaptive_chunker_preserves_source() -> None:
    """Use registered Go boundaries without losing original text."""

    first = (
        "package service\n"
        "\n"
        "type Worker struct{}\n"
        "\n"
        "func (w *Worker) First() int {\n" + "    return 1\n" * 8 + "}\n"
    )

    second = "\nfunc (w *Worker) Second() int {\n    return 2\n}\n"

    source = make_source(first + second)

    chunks = chunk_repository_source(
        source,
        max_tokens=len(first),
        token_counter=len,
    )

    assert len(chunks) > 1
    assert "".join(chunk.content for chunk in chunks) == source.content

    assert all(len(chunk.content) <= len(first) for chunk in chunks)

    symbols = {symbol for chunk in chunks for symbol in chunk.symbols}

    assert {"Worker.First", "Worker.Second"} <= symbols


def test_invalid_go_source_is_rejected() -> None:
    """Reject malformed Go source instead of trusting a partial parse."""

    source = make_source("package service\nfunc Broken( {\n")

    with pytest.raises(
        ValueError,
        match="Tree-sitter syntax error",
    ):
        analyze_go_source(source)
