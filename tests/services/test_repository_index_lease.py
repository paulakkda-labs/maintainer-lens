"""Unit tests for repository index build lease identities."""

from unittest.mock import AsyncMock

import pytest

from maintainer_lens.schemas.repository import RepositoryRevision
from maintainer_lens.services.repository_index_lease import (
    make_repository_index_identity_key,
    try_acquire_repository_index_lease,
)


def make_revision() -> RepositoryRevision:
    """Build a repository revision for lease tests."""

    return RepositoryRevision(
        repository="pypa/sampleproject",
        default_branch="main",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
    )


def make_key(
    *,
    model: str = "test-model",
    dimensions: int = 1024,
    chunker_version: str = "v1",
) -> str:
    """Generate a key for a fixed repository revision."""

    return make_repository_index_identity_key(
        make_revision(),
        embedding_provider_name="test-provider",
        embedding_model=model,
        embedding_dimensions=dimensions,
        chunker_version=chunker_version,
    )


def test_identity_key_is_deterministic() -> None:
    """Identical index configurations must share one lease identity."""

    assert make_key() == make_key()
    assert len(make_key()) == 64


def test_identity_key_changes_with_model() -> None:
    """Different embedding models must not share a lease."""

    assert make_key(model="model-a") != make_key(model="model-b")


def test_identity_key_changes_with_dimensions() -> None:
    """Different vector dimensions must not share a lease."""

    assert make_key(dimensions=768) != make_key(dimensions=1024)


def test_identity_key_changes_with_chunker_version() -> None:
    """Different chunking strategies must not share a lease."""

    assert make_key(chunker_version="v1") != make_key(chunker_version="v2")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("dimensions", "lease_seconds", "message"),
    [
        (0, 300, "dimensions must be positive"),
        (1024, 0, "duration must be positive"),
        (1024, -1, "duration must be positive"),
    ],
)
async def test_invalid_claim_arguments_do_not_access_database(
    dimensions: int,
    lease_seconds: int,
    message: str,
) -> None:
    """Reject invalid lease arguments before opening a transaction."""

    session = AsyncMock()

    with pytest.raises(ValueError, match=message):
        await try_acquire_repository_index_lease(
            session,
            make_revision(),
            embedding_provider_name="test-provider",
            embedding_model="test-model",
            embedding_dimensions=dimensions,
            chunker_version="v1",
            lease_seconds=lease_seconds,
        )

    session.begin.assert_not_called()
