"""Adaptive source chunking with language-aware boundaries."""

from collections.abc import Callable

from maintainer_lens.core.source_types import detect_source_type
from maintainer_lens.core.token_count import count_tokens
from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import RepositorySource
from maintainer_lens.services.markdown_structure import (
    MarkdownHeading,
    parse_markdown_headings,
)
from maintainer_lens.services.syntax_registry import analyze_source_syntax

MAX_WINDOW_LINES = 60
MAX_WINDOW_CHARS = 4000
MAX_CHUNK_TOKENS = 1024
OVERLAP_LINES = 8

TokenCounter = Callable[[str], int]


def _largest_fitting_end(
    lines: list[str],
    start: int,
    *,
    path: str,
    max_tokens: int,
    token_counter: TokenCounter,
) -> int:
    """Find the farthest line boundary satisfying all chunk budgets."""

    end = start
    content = ""

    while end < min(len(lines), start + MAX_WINDOW_LINES):
        candidate = content + lines[end]

        if len(candidate) > MAX_WINDOW_CHARS:
            break

        if token_counter(candidate) > max_tokens:
            break

        content = candidate
        end += 1

    if end == start:
        raise ValueError(f"Source line exceeds chunk budget: {path}:{start + 1}")

    return end


def _prefer_syntax_boundary(
    lines: list[str],
    start: int,
    largest_end: int,
    syntax_chunks: list[CodeChunk],
    token_counter: TokenCounter,
) -> int:
    """Prefer nearby syntax boundaries without creating tiny chunks."""

    candidates = sorted(
        {
            chunk.end_line
            for chunk in syntax_chunks
            if start < chunk.end_line <= largest_end
        },
        reverse=True,
    )

    if not candidates:
        return largest_end

    largest_tokens = token_counter("".join(lines[start:largest_end]))

    for end in candidates:
        candidate_tokens = token_counter("".join(lines[start:end]))

        if candidate_tokens * 5 >= largest_tokens * 3:
            return end

    return largest_end


def _syntax_metadata(
    syntax_chunks: list[CodeChunk],
    start_line: int,
    end_line: int,
) -> tuple[str, str | None, list[str]]:
    """Describe syntax symbols overlapping a source chunk."""

    overlapping = [
        chunk
        for chunk in syntax_chunks
        if chunk.symbol is not None
        and chunk.start_line <= end_line
        and chunk.end_line >= start_line
    ]

    symbols = list(
        dict.fromkeys(chunk.symbol for chunk in overlapping if chunk.symbol is not None)
    )

    exact = next(
        (
            chunk
            for chunk in overlapping
            if chunk.start_line == start_line and chunk.end_line == end_line
        ),
        None,
    )

    if exact is not None:
        return exact.kind, exact.symbol, symbols

    return "text", None, symbols


def _prefer_markdown_boundary(
    lines: list[str],
    start: int,
    largest_end: int,
    headings: list[MarkdownHeading],
    token_counter: TokenCounter,
) -> int:
    """Prefer splitting immediately before a Markdown heading."""

    candidates = sorted(
        {
            heading.start_line - 1
            for heading in headings
            if start < heading.start_line - 1 <= largest_end
        },
        reverse=True,
    )

    if not candidates:
        return largest_end

    largest_tokens = token_counter("".join(lines[start:largest_end]))

    for end in candidates:
        candidate_tokens = token_counter("".join(lines[start:end]))

        # Avoid tiny fragments when neighboring sections can fit together.
        if candidate_tokens * 5 >= largest_tokens * 3:
            return end

    return largest_end


def _markdown_path_at_line(
    headings: list[MarkdownHeading],
    line: int,
) -> list[str]:
    """Find the heading hierarchy active at a source line."""

    path: list[str] = []

    for heading in headings:
        if heading.start_line > line:
            break

        path = list(heading.path)

    return path


def chunk_repository_source(
    source: RepositorySource,
    *,
    max_tokens: int = MAX_CHUNK_TOKENS,
    token_counter: TokenCounter = count_tokens,
) -> list[CodeChunk]:
    """Chunk source text using token budgets and syntax metadata."""

    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")

    source_type = detect_source_type(source.file.path)

    if source_type is None:
        raise ValueError(f"Unsupported or excluded source: {source.file.path}")

    lines = source.content.splitlines(keepends=True)

    if not source.content.strip():
        return []

    # Language-specific parsing is handled by the registry.
    syntax_chunks = analyze_source_syntax(
        source,
        source_type.language,
    )

    # Markdown uses a separate heading-aware parser.
    markdown_headings = (
        parse_markdown_headings(source.content)
        if source_type.language == "markdown"
        else []
    )

    # Keep small files intact whenever they satisfy every budget.
    if (
        len(lines) <= MAX_WINDOW_LINES
        and len(source.content) <= MAX_WINDOW_CHARS
        and token_counter(source.content) <= max_tokens
    ):
        return [
            CodeChunk(
                file=source.file,
                language=source_type.language,
                kind="file",
                symbols=list(
                    dict.fromkeys(
                        chunk.symbol
                        for chunk in syntax_chunks
                        if chunk.symbol is not None
                    )
                ),
                headings=[heading.title for heading in markdown_headings],
                start_line=1,
                end_line=len(lines),
                content=source.content,
            )
        ]

    chunks: list[CodeChunk] = []
    start = 0

    heading_boundaries = {heading.start_line - 1 for heading in markdown_headings}

    while start < len(lines):
        end = _largest_fitting_end(
            lines,
            start,
            path=source.file.path,
            max_tokens=max_tokens,
            token_counter=token_counter,
        )

        heading_path: list[str] = []
        chunk_headings: list[str] = []

        if syntax_chunks:
            # Works for every registered syntax analyzer.
            end = _prefer_syntax_boundary(
                lines,
                start,
                end,
                syntax_chunks,
                token_counter,
            )

            kind, symbol, symbols = _syntax_metadata(
                syntax_chunks,
                start + 1,
                end,
            )

        elif source_type.language == "markdown":
            end = _prefer_markdown_boundary(
                lines,
                start,
                end,
                markdown_headings,
                token_counter,
            )

            heading_path = _markdown_path_at_line(
                markdown_headings,
                start + 1,
            )

            chunk_headings = [
                heading.title
                for heading in markdown_headings
                if start < heading.start_line <= end
            ]

            kind = "section" if heading_path else "text"
            symbol = None
            symbols = []

        else:
            # Fallback for languages without recognized syntax structures.
            kind = "text"
            symbol = None
            symbols = []

        chunks.append(
            CodeChunk(
                file=source.file,
                language=source_type.language,
                kind=kind,
                symbol=symbol,
                symbols=symbols,
                heading_path=heading_path,
                headings=chunk_headings,
                start_line=start + 1,
                end_line=end,
                content="".join(lines[start:end]),
            )
        )

        if end == len(lines):
            break

        # Avoid overlap for syntax-aware chunks and heading boundaries.
        if syntax_chunks or (
            source_type.language == "markdown" and end in heading_boundaries
        ):
            start = end

        else:
            # Preserve context in plain-text fallback windows.
            window_lines = end - start
            overlap = min(
                OVERLAP_LINES,
                window_lines // 4,
            )
            start = end - overlap

    return chunks
