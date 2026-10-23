"""Extract Markdown heading hierarchy and source line positions."""

from dataclasses import dataclass

from markdown_it import MarkdownIt


@dataclass(frozen=True)
class MarkdownHeading:
    """A document heading with its complete parent hierarchy."""

    start_line: int
    level: int
    title: str
    path: tuple[str, ...]


def parse_markdown_headings(content: str) -> list[MarkdownHeading]:
    """Parse Markdown heading structure without rendering the document."""

    parser = MarkdownIt("commonmark")
    tokens = parser.parse(content)

    headings: list[MarkdownHeading] = []
    parents: list[tuple[int, str]] = []

    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue

        level = int(token.tag[1:])
        start_line = token.map[0] + 1

        # The inline token immediately follows heading_open.
        title = tokens[index + 1].content.strip()

        while parents and parents[-1][0] >= level:
            parents.pop()

        parents.append((level, title))

        headings.append(
            MarkdownHeading(
                start_line=start_line,
                level=level,
                title=title,
                path=tuple(name for _, name in parents),
            )
        )

    return headings
