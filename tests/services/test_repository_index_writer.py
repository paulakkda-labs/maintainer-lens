"""Unit tests for repository index write preparation."""

import pytest

from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import RepositoryFile
from maintainer_lens.services.repository_index_writer import (
    EmbeddedChunk,
    _prepare_items,
    make_chunk_key,
)


def make_chunk(
    *,
    content: str = "def add_one(number):\n    return number + 1\n",
    language: str = "python",
) -> CodeChunk:
    """Build one source chunk for persistence tests."""

    return CodeChunk(
        file=RepositoryFile(
            path="src/sample.py",
            blob_sha="a" * 40,
            size=len(content.encode("utf-8")),
            mode="100644",
        ),
        language=language,
        kind="function",
        symbol="add_one",
        symbols=["add_one"],
        start_line=1,
        end_line=2,
        content=content,
    )


def test_chunk_key_is_deterministic() -> None:
    """Identical source chunks must have the same stable key."""

    first = make_chunk()
    second = make_chunk()

    assert make_chunk_key(first) == make_chunk_key(second)
    assert len(make_chunk_key(first)) == 64


def test_chunk_key_changes_with_source_content() -> None:
    """Different source content must produce different identities."""

    first = make_chunk()
    second = make_chunk(content="def add_one(number):\n    return number + 2\n")

    assert make_chunk_key(first) != make_chunk_key(second)


@pytest.mark.parametrize(
    ("vector", "dimensions", "message"),
    [
        ([0.1, 0.2], 3, "dimension mismatch"),
        ([float("nan"), 0.2], 2, "non-finite"),
        ([float("inf"), 0.2], 2, "non-finite"),
    ],
)
def test_prepare_rejects_invalid_vectors(
    vector: list[float],
    dimensions: int,
    message: str,
) -> None:
    """Validate vectors before opening a database transaction."""

    with pytest.raises(ValueError, match=message):
        _prepare_items(
            [EmbeddedChunk(make_chunk(), vector)],
            dimensions,
        )


def test_prepare_rejects_duplicate_chunks() -> None:
    """Do not accept duplicate logical chunks within one write."""

    item = EmbeddedChunk(make_chunk(), [0.1, 0.2])

    with pytest.raises(ValueError, match="Duplicate chunk key"):
        _prepare_items([item, item], 2)


def test_prepare_rejects_empty_index() -> None:
    """A ready index must not be created from an empty input."""

    with pytest.raises(ValueError, match="at least one chunk"):
        _prepare_items([], 1024)
