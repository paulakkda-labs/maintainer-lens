"""Extract JavaScript and TypeScript source structure using Tree-sitter."""

from functools import lru_cache
from pathlib import PurePosixPath

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import RepositorySource

DECLARATION_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "lexical_declaration",
        "variable_declaration",
    }
)

FUNCTION_EXPRESSIONS = frozenset(
    {
        "arrow_function",
        "function_expression",
        "generator_function",
    }
)


@lru_cache(maxsize=4)
def _get_language(suffix: str) -> Language:
    """Load the grammar matching a source file extension."""

    if suffix in {".js", ".jsx"}:
        return Language(tree_sitter_javascript.language())

    if suffix == ".ts":
        return Language(tree_sitter_typescript.language_typescript())

    if suffix == ".tsx":
        return Language(tree_sitter_typescript.language_tsx())

    raise ValueError(f"Unsupported Tree-sitter source suffix: {suffix}")


def _node_text(node: Node | None, raw: bytes) -> str | None:
    """Read the original UTF-8 bytes belonging to a syntax node."""

    if node is None:
        return None

    return raw[node.start_byte : node.end_byte].decode("utf-8")


def _is_async(node: Node) -> bool:
    """Check whether a declaration has a direct async modifier."""

    return any(child.type == "async" for child in node.children)


def _append_structure(
    results: list[CodeChunk],
    source: RepositorySource,
    lines: list[str],
    *,
    node: Node,
    kind: str,
    symbol: str | None,
) -> None:
    """Store syntax metadata with original source line boundaries."""

    start_line = node.start_point.row + 1
    end_line = node.end_point.row

    if node.end_point.column > 0:
        end_line += 1

    if end_line < start_line:
        raise ValueError(f"Invalid Tree-sitter source range: {source.file.path}")

    results.append(
        CodeChunk(
            file=source.file,
            language=(
                "typescript"
                if PurePosixPath(source.file.path).suffix.lower() in {".ts", ".tsx"}
                else "javascript"
            ),
            kind=kind,
            symbol=symbol,
            symbols=[symbol] if symbol is not None else [],
            start_line=start_line,
            end_line=end_line,
            content="".join(lines[start_line - 1 : end_line]),
        )
    )


def _top_level_declaration(node: Node) -> tuple[Node, Node] | None:
    """Unwrap an exported declaration while retaining its source range."""

    if node.type in DECLARATION_TYPES:
        return node, node

    if node.type != "export_statement":
        return None

    for child in node.named_children:
        if child.type in DECLARATION_TYPES:
            return child, node

    return None


def analyze_js_ts_source(source: RepositorySource) -> list[CodeChunk]:
    """Extract declaration and method boundaries for JS, JSX, TS, and TSX."""

    suffix = PurePosixPath(source.file.path).suffix.lower()
    language = _get_language(suffix)

    raw = source.content.encode("utf-8")
    lines = source.content.splitlines(keepends=True)

    parser = Parser(language)
    tree = parser.parse(raw)

    if tree.root_node.has_error:
        raise ValueError(f"Tree-sitter syntax error: {source.file.path}")

    results: list[CodeChunk] = []

    for top in tree.root_node.named_children:
        declaration = _top_level_declaration(top)

        if declaration is None:
            continue

        node, outer = declaration

        if node.type in {
            "function_declaration",
            "generator_function_declaration",
        }:
            name = _node_text(
                node.child_by_field_name("name"),
                raw,
            )

            _append_structure(
                results,
                source,
                lines,
                node=outer,
                kind=("async_function" if _is_async(node) else "function"),
                symbol=name,
            )

        elif node.type in {
            "class_declaration",
            "abstract_class_declaration",
        }:
            class_name = _node_text(
                node.child_by_field_name("name"),
                raw,
            )

            _append_structure(
                results,
                source,
                lines,
                node=outer,
                kind="class",
                symbol=class_name,
            )

            body = node.child_by_field_name("body")

            if body is None or class_name is None:
                continue

            for member in body.named_children:
                if member.type not in {
                    "method_definition",
                    "abstract_method_signature",
                }:
                    continue

                method_name = _node_text(
                    member.child_by_field_name("name"),
                    raw,
                )

                if method_name is None:
                    continue

                _append_structure(
                    results,
                    source,
                    lines,
                    node=member,
                    kind=("async_method" if _is_async(member) else "method"),
                    symbol=f"{class_name}.{method_name}",
                )

        elif node.type in {
            "interface_declaration",
            "type_alias_declaration",
        }:
            name = _node_text(
                node.child_by_field_name("name"),
                raw,
            )

            _append_structure(
                results,
                source,
                lines,
                node=outer,
                kind=(
                    "interface"
                    if node.type == "interface_declaration"
                    else "type_alias"
                ),
                symbol=name,
            )

        elif node.type in {
            "lexical_declaration",
            "variable_declaration",
        }:
            declarators = [
                child
                for child in node.named_children
                if child.type == "variable_declarator"
            ]

            # Only classify unambiguous single-variable functions.
            if len(declarators) != 1:
                continue

            declarator = declarators[0]
            value = declarator.child_by_field_name("value")

            if value is None or value.type not in FUNCTION_EXPRESSIONS:
                continue

            name = _node_text(
                declarator.child_by_field_name("name"),
                raw,
            )

            if name is None:
                continue

            _append_structure(
                results,
                source,
                lines,
                node=outer,
                kind=("async_function" if _is_async(value) else "function"),
                symbol=name,
            )

    return results
