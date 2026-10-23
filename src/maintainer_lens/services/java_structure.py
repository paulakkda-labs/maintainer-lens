"""Extract Java declarations and methods using Tree-sitter."""

from functools import lru_cache
from typing import Literal

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import RepositorySource

TYPE_DECLARATIONS = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "record_declaration",
    }
)

MEMBER_DECLARATIONS = frozenset(
    {
        "method_declaration",
        "constructor_declaration",
        "compact_constructor_declaration",
    }
)

StructureKind = Literal["class", "interface", "method"]


@lru_cache(maxsize=1)
def _get_java_language() -> Language:
    """Load the Java grammar once per process."""

    return Language(tree_sitter_java.language())


def _node_text(node: Node | None, raw: bytes) -> str | None:
    """Read a syntax node's original UTF-8 text."""

    if node is None:
        return None

    return raw[node.start_byte : node.end_byte].decode("utf-8")


def _make_structure(
    source: RepositorySource,
    lines: list[str],
    node: Node,
    *,
    kind: StructureKind,
    symbol: str,
) -> CodeChunk:
    """Create source-location metadata for a Java declaration."""

    start_line = node.start_point.row + 1
    end_line = node.end_point.row

    if node.end_point.column > 0:
        end_line += 1

    return CodeChunk(
        file=source.file,
        language="java",
        kind=kind,
        symbol=symbol,
        symbols=[symbol],
        start_line=start_line,
        end_line=end_line,
        content="".join(lines[start_line - 1 : end_line]),
    )


def analyze_java_source(source: RepositorySource) -> list[CodeChunk]:
    """Extract top-level Java types and their direct methods."""

    if not source.file.path.lower().endswith(".java"):
        raise ValueError(f"Not a Java source file: {source.file.path}")

    raw = source.content.encode("utf-8")
    lines = source.content.splitlines(keepends=True)

    parser = Parser(_get_java_language())
    tree = parser.parse(raw)

    if tree.root_node.has_error:
        raise ValueError(f"Tree-sitter syntax error: {source.file.path}")

    structures: list[CodeChunk] = []

    for node in tree.root_node.named_children:
        if node.type not in TYPE_DECLARATIONS:
            continue

        type_name = _node_text(
            node.child_by_field_name("name"),
            raw,
        )

        if type_name is None:
            continue

        type_kind: StructureKind = (
            "interface" if node.type == "interface_declaration" else "class"
        )

        structures.append(
            _make_structure(
                source,
                lines,
                node,
                kind=type_kind,
                symbol=type_name,
            )
        )

        body = node.child_by_field_name("body")

        if body is None:
            continue

        for member in body.named_children:
            if member.type not in MEMBER_DECLARATIONS:
                continue

            member_name = _node_text(
                member.child_by_field_name("name"),
                raw,
            )

            if member_name is None:
                continue

            structures.append(
                _make_structure(
                    source,
                    lines,
                    member,
                    kind="method",
                    symbol=f"{type_name}.{member_name}",
                )
            )

    return structures
