"""Extract top-level Python chunks and class method structure."""

import ast
from pathlib import PurePosixPath
from typing import Literal

from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import RepositorySource

ChunkKind = Literal[
    "module",
    "function",
    "async_function",
    "class",
    "method",
    "async_method",
]

DefinitionNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _definition_start(node: DefinitionNode) -> int:
    """Include decorators when locating a definition."""

    start = node.lineno

    for decorator in node.decorator_list:
        start = min(start, decorator.lineno)

    return start


def _make_chunk(
    source: RepositorySource,
    lines: list[str],
    *,
    start_line: int,
    end_line: int,
    kind: ChunkKind,
    symbol: str | None = None,
) -> CodeChunk | None:
    """Create a source chunk without rewriting the original code."""

    while start_line <= end_line and not lines[start_line - 1].strip():
        start_line += 1

    while end_line >= start_line and not lines[end_line - 1].strip():
        end_line -= 1

    if start_line > end_line:
        return None

    return CodeChunk(
        file=source.file,
        language="python",
        kind=kind,
        symbol=symbol,
        symbols=[symbol] if symbol is not None else [],
        start_line=start_line,
        end_line=end_line,
        content="".join(lines[start_line - 1 : end_line]),
    )


def analyze_python_source(
    source: RepositorySource,
) -> tuple[list[CodeChunk], list[CodeChunk]]:
    """Extract top-level chunks and direct class methods in one AST pass."""

    suffix = PurePosixPath(source.file.path).suffix.lower()

    if suffix not in {".py", ".pyi"}:
        raise ValueError(f"Not a Python source file: {source.file.path}")

    tree = ast.parse(
        source.content,
        filename=source.file.path,
        type_comments=True,
    )

    lines = source.content.splitlines(keepends=True)

    top_level: list[CodeChunk] = []
    methods: list[CodeChunk] = []
    cursor = 1

    for node in tree.body:
        if not isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue

        start_line = _definition_start(node)

        if node.end_lineno is None:
            raise ValueError(f"Missing AST end line: {source.file.path}")

        preceding = _make_chunk(
            source,
            lines,
            start_line=cursor,
            end_line=start_line - 1,
            kind="module",
        )

        if preceding is not None:
            top_level.append(preceding)

        if isinstance(node, ast.AsyncFunctionDef):
            kind: ChunkKind = "async_function"
        elif isinstance(node, ast.FunctionDef):
            kind = "function"
        else:
            kind = "class"

        definition = _make_chunk(
            source,
            lines,
            start_line=start_line,
            end_line=node.end_lineno,
            kind=kind,
            symbol=node.name,
        )

        if definition is not None:
            top_level.append(definition)

        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if not isinstance(
                    member,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    continue

                if member.end_lineno is None:
                    raise ValueError(f"Missing method end line: {source.file.path}")

                method_kind: ChunkKind = (
                    "async_method"
                    if isinstance(member, ast.AsyncFunctionDef)
                    else "method"
                )

                method = _make_chunk(
                    source,
                    lines,
                    start_line=_definition_start(member),
                    end_line=member.end_lineno,
                    kind=method_kind,
                    symbol=f"{node.name}.{member.name}",
                )

                if method is not None:
                    methods.append(method)

        cursor = node.end_lineno + 1

    remaining = _make_chunk(
        source,
        lines,
        start_line=cursor,
        end_line=len(lines),
        kind="module",
    )

    if remaining is not None:
        top_level.append(remaining)

    return top_level, methods


def chunk_python_source(source: RepositorySource) -> list[CodeChunk]:
    """Preserve the existing top-level Python chunking interface."""

    top_level, _ = analyze_python_source(source)
    return top_level
