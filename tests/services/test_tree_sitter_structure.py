"""Tests for JavaScript and TypeScript structural source extraction."""

import pytest

from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositorySource,
)
from maintainer_lens.services.source_chunker import (
    chunk_repository_source,
)
from maintainer_lens.services.tree_sitter_structure import (
    analyze_js_ts_source,
)


def make_source(path: str, content: str) -> RepositorySource:
    """Build an in-memory source file for parser tests."""

    return RepositorySource(
        file=RepositoryFile(
            path=path,
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        content=content,
    )


def test_javascript_detects_functions_classes_and_methods() -> None:
    """Recognize exported functions, arrow functions, and class methods."""

    source = make_source(
        "src/service.js",
        "export function fetchData() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "const normalize = (value) => value.trim();\n"
        "\n"
        "class Service {\n"
        "  async load() {\n"
        "    return fetchData();\n"
        "  }\n"
        "}\n",
    )

    structures = analyze_js_ts_source(source)

    assert {(item.kind, item.symbol) for item in structures} == {
        ("function", "fetchData"),
        ("function", "normalize"),
        ("class", "Service"),
        ("async_method", "Service.load"),
    }

    function = next(item for item in structures if item.symbol == "fetchData")

    assert (function.start_line, function.end_line) == (1, 3)
    assert function.content.startswith("export function")


def test_typescript_detects_types_and_class_methods() -> None:
    """Recognize TypeScript interfaces, type aliases, and methods."""

    source = make_source(
        "src/service.ts",
        "export interface Config {\n"
        "  enabled: boolean;\n"
        "}\n"
        "\n"
        "export type Id = string;\n"
        "\n"
        "export class Service {\n"
        "  async load(): Promise<Id> {\n"
        '    return "123";\n'
        "  }\n"
        "}\n",
    )

    structures = analyze_js_ts_source(source)

    assert {(item.kind, item.symbol) for item in structures} == {
        ("interface", "Config"),
        ("type_alias", "Id"),
        ("class", "Service"),
        ("async_method", "Service.load"),
    }


@pytest.mark.parametrize(
    "path",
    ["src/Card.jsx", "src/Card.tsx"],
)
def test_jsx_and_tsx_parsing(path: str) -> None:
    """Use the correct grammar for JSX and TSX files."""

    source = make_source(
        path,
        "export function Card() {\n  return <div>Hello</div>;\n}\n",
    )

    structures = analyze_js_ts_source(source)

    assert len(structures) == 1
    assert structures[0].symbol == "Card"


def test_adaptive_chunker_uses_javascript_method_boundaries() -> None:
    """Preserve full source while preferring known method boundaries."""

    first_block = (
        "export class Service {\n  first() {\n" + "    return 1;\n" * 8 + "  }\n"
    )

    remaining = "  second() {\n    return 2;\n  }\n}\n"

    source = make_source(
        "src/service.js",
        first_block + remaining,
    )

    budget = len(first_block)

    chunks = chunk_repository_source(
        source,
        max_tokens=budget,
        token_counter=len,
    )

    assert len(chunks) == 2
    assert chunks[0].end_line == 11
    assert "Service.first" in chunks[0].symbols
    assert "Service.second" in chunks[1].symbols

    assert "".join(chunk.content for chunk in chunks) == source.content
    assert all(len(chunk.content) <= budget for chunk in chunks)


def test_invalid_typescript_is_rejected() -> None:
    """Do not trust partial syntax trees containing parse errors."""

    source = make_source(
        "src/broken.ts",
        "export function broken( {\n",
    )

    with pytest.raises(ValueError, match="Tree-sitter syntax error"):
        analyze_js_ts_source(source)
