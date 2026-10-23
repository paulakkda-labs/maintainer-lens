"""Tests for Java Tree-sitter structural analysis."""

import pytest

from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositorySource,
)
from maintainer_lens.services.java_structure import analyze_java_source
from maintainer_lens.services.source_chunker import chunk_repository_source


def make_source(content: str) -> RepositorySource:
    """Create an in-memory Java source file."""

    return RepositorySource(
        file=RepositoryFile(
            path="src/UserService.java",
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        content=content,
    )


def test_java_class_constructor_and_annotated_method() -> None:
    """Recognize a class, constructor, and annotated method."""

    source = make_source(
        "package demo;\n"
        "\n"
        "public class UserService {\n"
        "    public UserService() {}\n"
        "\n"
        "    @Deprecated\n"
        "    public String getUser(String id) {\n"
        "        return id;\n"
        "    }\n"
        "}\n"
    )

    structures = analyze_java_source(source)

    assert [(item.kind, item.symbol) for item in structures] == [
        ("class", "UserService"),
        ("method", "UserService.UserService"),
        ("method", "UserService.getUser"),
    ]

    assert (
        structures[2].start_line,
        structures[2].end_line,
    ) == (6, 9)

    assert structures[2].content.startswith("    @Deprecated\n")


def test_java_interface_and_record() -> None:
    """Recognize interface members and Java record methods."""

    source = make_source(
        "public interface Greeter {\n"
        "    String greet(String name);\n"
        "}\n"
        "\n"
        "public record Point(int x) {\n"
        "    public int doubled() {\n"
        "        return x * 2;\n"
        "    }\n"
        "}\n"
    )

    structures = analyze_java_source(source)

    assert [(item.kind, item.symbol) for item in structures] == [
        ("interface", "Greeter"),
        ("method", "Greeter.greet"),
        ("class", "Point"),
        ("method", "Point.doubled"),
    ]


def test_java_adaptive_chunker_preserves_source() -> None:
    """Use method boundaries while keeping the original source intact."""

    first = (
        "public class Service {\n"
        "    public int first() {\n"
        "        int value = 0;\n"
        + "        value += 1;\n" * 6
        + "        return value;\n"
        + "    }\n"
    )

    second = "    public int second() {\n        return 2;\n    }\n}\n"

    source = make_source(first + second)

    chunks = chunk_repository_source(
        source,
        max_tokens=len(first),
        token_counter=len,
    )

    assert len(chunks) == 2
    assert chunks[0].end_line == len(first.splitlines())

    assert "".join(chunk.content for chunk in chunks) == source.content

    assert all(len(chunk.content) <= len(first) for chunk in chunks)

    symbols = {symbol for chunk in chunks for symbol in chunk.symbols}

    assert "Service.first" in symbols
    assert "Service.second" in symbols


def test_invalid_java_source_is_rejected() -> None:
    """Reject malformed Java syntax."""

    source = make_source("public class Broken {\n    public void run( {\n")

    with pytest.raises(
        ValueError,
        match="Tree-sitter syntax error",
    ):
        analyze_java_source(source)
