"""Extract Go function and method structure using Tree-sitter."""

from functools import lru_cache

import tree_sitter_go
from tree_sitter import Language, Node, Parser

from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import RepositorySource


@lru_cache(maxsize=1)
def _get_go_language() -> Language:
    """Load the Go grammar once."""

    return Language(tree_sitter_go.language())


def _node_text(node: Node, raw: bytes) -> str:
    """Read the original source text for a syntax node."""

    return raw[node.start_byte : node.end_byte].decode("utf-8")


def _receiver_type_name(node: Node, raw: bytes) -> str | None:
    """Extract the receiver's underlying type name."""

    receiver = node.child_by_field_name("receiver")

    if receiver is None:
        return None

    parameter = next(
        (
            child
            for child in receiver.named_children
            if child.type == "parameter_declaration"
        ),
        None,
    )

    if parameter is None:
        return None

    type_node = parameter.child_by_field_name("type")

    if type_node is None:
        return None

    pending = [type_node]

    while pending:
        current = pending.pop()

        if current.type == "type_identifier":
            return _node_text(current, raw)

        pending.extend(reversed(current.named_children))

    return None


def analyze_go_source(source: RepositorySource) -> list[CodeChunk]:
    """Extract named top-level Go functions and methods."""

    if not source.file.path.lower().endswith(".go"):
        raise ValueError(f"Not a Go source file: {source.file.path}")

    raw = source.content.encode("utf-8")
    lines = source.content.splitlines(keepends=True)

    parser = Parser(_get_go_language())
    tree = parser.parse(raw)

    if tree.root_node.has_error:
        raise ValueError(f"Tree-sitter syntax error: {source.file.path}")

    results: list[CodeChunk] = []

    for node in tree.root_node.named_children:
        if node.type not in {
            "function_declaration",
            "method_declaration",
        }:
            continue

        name_node = node.child_by_field_name("name")

        if name_node is None:
            continue

        name = _node_text(name_node, raw)

        if node.type == "method_declaration":
            receiver_type = _receiver_type_name(node, raw)

            if receiver_type is None:
                continue

            kind = "method"
            symbol = f"{receiver_type}.{name}"
        else:
            kind = "function"
            symbol = name

        start_line = node.start_point.row + 1
        end_line = node.end_point.row

        if node.end_point.column > 0:
            end_line += 1

        results.append(
            CodeChunk(
                file=source.file,
                language="go",
                kind=kind,
                symbol=symbol,
                symbols=[symbol],
                start_line=start_line,
                end_line=end_line,
                content="".join(lines[start_line - 1 : end_line]),
            )
        )

    return results
