"""Tests for Python class method-aware chunking."""

from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositorySource,
)
from maintainer_lens.services.python_chunker import (
    analyze_python_source,
    chunk_python_source,
)
from maintainer_lens.services.source_chunker import (
    chunk_repository_source,
)


def make_source(content: str) -> RepositorySource:
    """Create a Python source model for structural tests."""

    return RepositorySource(
        file=RepositoryFile(
            path="src/service.py",
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        content=content,
    )


def test_analyze_class_methods_including_decorators() -> None:
    """Recognize method boundaries and qualified symbol names."""

    source = make_source(
        "class Service:\n"
        "    @trace(\n"
        "        enabled=True,\n"
        "    )\n"
        "    async def run(self):\n"
        "        return 1\n"
        "\n"
        "    def stop(self):\n"
        "        return 2\n"
    )

    top_level, methods = analyze_python_source(source)

    assert len(top_level) == 1
    assert top_level[0].kind == "class"
    assert top_level[0].symbol == "Service"

    assert [
        (method.kind, method.symbol, method.start_line, method.end_line)
        for method in methods
    ] == [
        ("async_method", "Service.run", 2, 6),
        ("method", "Service.stop", 8, 9),
    ]

    assert methods[0].content.startswith("    @trace(\n")
    assert methods[1].content.startswith("    def stop(self):\n")


def test_existing_python_chunker_remains_compatible() -> None:
    """Keep the original top-level chunking behavior."""

    source = make_source(
        "class Service:\n"
        "    def first(self):\n"
        "        return 1\n"
        "    def second(self):\n"
        "        return 2\n"
    )

    chunks = chunk_python_source(source)

    assert len(chunks) == 1
    assert chunks[0].kind == "class"
    assert chunks[0].symbol == "Service"
    assert chunks[0].content == source.content


def test_adaptive_chunker_splits_at_method_boundary() -> None:
    """Prefer a complete method and preserve qualified metadata."""

    first_block = "class Service:\n    def first(self):\n" + "        return 1\n" * 8

    second_method = "    @classmethod\n    async def second(cls):\n        return 2\n"

    source = make_source(first_block + second_method)

    chunks = chunk_repository_source(
        source,
        max_tokens=len(first_block) + 1,
        token_counter=len,
    )

    assert len(chunks) == 2

    assert chunks[0].end_line == 10
    assert "Service.first" in chunks[0].symbols

    assert chunks[1].kind == "async_method"
    assert chunks[1].symbol == "Service.second"
    assert (chunks[1].start_line, chunks[1].end_line) == (11, 13)

    assert "".join(chunk.content for chunk in chunks) == source.content

    assert all(len(chunk.content) <= len(first_block) + 1 for chunk in chunks)


def test_nested_function_is_not_a_class_method() -> None:
    """Do not mistake a nested local function for a class method."""

    source = make_source(
        "class Worker:\n"
        "    def run(self):\n"
        "        def helper():\n"
        "            return 1\n"
        "        return helper()\n"
    )

    _, methods = analyze_python_source(source)

    assert len(methods) == 1
    assert methods[0].symbol == "Worker.run"


def test_oversized_method_still_respects_budget() -> None:
    """Allow fallback splits inside a method without losing source."""

    content = "class Service:\n    def huge(self):\n" + "        value = 1\n" * 25
    source = make_source(content)

    chunks = chunk_repository_source(
        source,
        max_tokens=65,
        token_counter=len,
    )

    assert len(chunks) > 1
    assert "".join(chunk.content for chunk in chunks) == content

    assert all(len(chunk.content) <= 65 for chunk in chunks)
    assert all("Service.huge" in chunk.symbols for chunk in chunks)
