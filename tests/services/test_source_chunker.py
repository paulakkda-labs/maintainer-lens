"""Tests for source format detection and baseline chunk routing."""

import pytest

from maintainer_lens.core.source_types import detect_source_type
from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositorySource,
)
from maintainer_lens.services.source_chunker import (
    chunk_repository_source,
)


def make_source(
    path: str,
    content: str,
) -> RepositorySource:
    return RepositorySource(
        file=RepositoryFile(
            path=path,
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        content=content,
    )


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("src/main.py", "python"),
        ("src/index.ts", "typescript"),
        ("src/main.go", "go"),
        ("README.md", "markdown"),
        ("package.json", "json"),
        ("Dockerfile", "dockerfile"),
        ("schema.sql", "sql"),
    ],
)
def test_detect_source_types(path: str, language: str) -> None:
    result = detect_source_type(path)

    assert result is not None
    assert result.language == language


@pytest.mark.parametrize(
    "path",
    [".env", ".env.production", "assets/logo.png", "uv.lock"],
)
def test_reject_excluded_source_types(path: str) -> None:
    assert detect_source_type(path) is None


def test_small_python_file_keeps_whole_file() -> None:
    source = make_source(
        "src/sample.py",
        "def first():\n    return 1\n\ndef second():\n    return first()\n",
    )

    chunks = chunk_repository_source(source)

    assert len(chunks) == 1
    assert chunks[0].kind == "file"
    assert chunks[0].language == "python"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 5
    assert chunks[0].content == source.content


def test_typescript_line_windows_preserve_positions() -> None:
    content = "".join(f"const value{i} = {i};\n" for i in range(85))
    source = make_source("src/index.ts", content)

    chunks = chunk_repository_source(source)

    assert len(chunks) == 2
    assert chunks[0].language == "typescript"
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 60)
    assert (chunks[1].start_line, chunks[1].end_line) == (53, 85)
    assert chunks[0].content.startswith("const value0")
    assert chunks[1].content.endswith("const value84 = 84;\n")


def test_invalid_python_still_raises_syntax_error() -> None:
    source = make_source(
        "src/broken.py",
        "def broken(:\n    pass\n",
    )

    with pytest.raises(SyntaxError):
        chunk_repository_source(source)


def test_reject_unsupported_source() -> None:
    source = make_source("assets/image.png", "not an image")

    with pytest.raises(ValueError, match="Unsupported"):
        chunk_repository_source(source)


def test_reject_oversized_single_line() -> None:
    source = make_source("src/index.js", "x" * 5000)

    with pytest.raises(ValueError, match="chunk budget"):
        chunk_repository_source(source)
