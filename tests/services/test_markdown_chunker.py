"""Tests for Markdown-aware source chunking."""

from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositorySource,
)
from maintainer_lens.services.markdown_structure import (
    parse_markdown_headings,
)
from maintainer_lens.services.source_chunker import (
    chunk_repository_source,
)


def make_source(content: str) -> RepositorySource:
    """Build an in-memory README source for chunking tests."""

    return RepositorySource(
        file=RepositoryFile(
            path="README.md",
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        content=content,
    )


def test_markdown_heading_hierarchy_ignores_code_fences() -> None:
    """Recognize heading hierarchy without treating code as headings."""

    content = (
        "# Guide\n"
        "```python\n"
        "## Not a heading\n"
        "```\n"
        "Installation\n"
        "------------\n"
        "Details\n"
        "~~~\n"
        "### Also not a heading\n"
        "~~~\n"
        "### Docker\n"
        "Run the container.\n"
    )

    headings = parse_markdown_headings(content)

    assert [
        (heading.start_line, heading.level, heading.title) for heading in headings
    ] == [
        (1, 1, "Guide"),
        (5, 2, "Installation"),
        (11, 3, "Docker"),
    ]

    assert headings[-1].path == (
        "Guide",
        "Installation",
        "Docker",
    )


def test_small_markdown_file_stays_whole() -> None:
    """Preserve a small document and its headings in a single chunk."""

    source = make_source(
        "# Guide\n"
        "Introduction.\n"
        "\n"
        "## Installation\n"
        "Install the package.\n"
        "\n"
        "## Usage\n"
        "Run the application.\n"
    )

    chunks = chunk_repository_source(source)

    assert len(chunks) == 1
    assert chunks[0].kind == "file"
    assert chunks[0].language == "markdown"
    assert chunks[0].headings == [
        "Guide",
        "Installation",
        "Usage",
    ]
    assert chunks[0].content == source.content


def test_markdown_prefers_section_boundaries() -> None:
    """Prefer headings while merging content within token budgets."""

    content = (
        "# Guide\n"
        + "intro\n" * 12
        + "## Install\n"
        + "run\n" * 12
        + "## Fixes\n"
        + "fix\n" * 12
    )

    source = make_source(content)

    chunks = chunk_repository_source(
        source,
        max_tokens=95,
        token_counter=len,
    )

    assert [(chunk.start_line, chunk.end_line) for chunk in chunks] == [
        (1, 13),
        (14, 26),
        (27, 39),
    ]

    assert [chunk.heading_path for chunk in chunks] == [
        ["Guide"],
        ["Guide", "Install"],
        ["Guide", "Fixes"],
    ]

    assert [chunk.headings for chunk in chunks] == [
        ["Guide"],
        ["Install"],
        ["Fixes"],
    ]

    assert all(chunk.kind == "section" for chunk in chunks)
    assert all(len(chunk.content) <= 95 for chunk in chunks)

    # Heading-aligned chunks have no overlap in this example.
    assert "".join(chunk.content for chunk in chunks) == content


def test_long_markdown_section_preserves_coverage() -> None:
    """Split a long section without losing its heading context."""

    content = "# Guide\n" + "long text\n" * 35
    source = make_source(content)

    chunks = chunk_repository_source(
        source,
        max_tokens=40,
        token_counter=len,
    )

    assert len(chunks) > 1

    original_lines = content.splitlines(keepends=True)
    covered_lines: set[int] = set()

    for chunk in chunks:
        assert len(chunk.content) <= 40
        assert chunk.heading_path == ["Guide"]

        assert chunk.content == "".join(
            original_lines[chunk.start_line - 1 : chunk.end_line]
        )

        covered_lines.update(range(chunk.start_line, chunk.end_line + 1))

    assert covered_lines == set(range(1, len(original_lines) + 1))


def test_markdown_small_sections_can_be_merged() -> None:
    """Avoid fragmenting short neighboring document sections."""

    source = make_source(
        "# Project\n"
        "Overview.\n"
        "## Install\n"
        "Install instructions.\n"
        "## Usage\n"
        "Usage instructions.\n"
    )

    chunks = chunk_repository_source(
        source,
        max_tokens=200,
        token_counter=len,
    )

    assert len(chunks) == 1
    assert chunks[0].headings == [
        "Project",
        "Install",
        "Usage",
    ]
