"""Tests for repository index SQLAlchemy model metadata."""

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from maintainer_lens.db.base import Base
from maintainer_lens.db.models.repository_index import (
    RepositoryChunk,
    RepositoryIndex,
)


def test_repository_tables_share_existing_metadata() -> None:
    """Register repository tables on the application's declarative base."""

    assert Base.metadata.tables["repository_indexes"] is RepositoryIndex.__table__
    assert Base.metadata.tables["repository_chunks"] is RepositoryChunk.__table__


def test_repository_index_identity_is_unique() -> None:
    """Prevent duplicate index identities for a repository revision."""

    constraints = [
        constraint
        for constraint in RepositoryIndex.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    identity = next(
        constraint
        for constraint in constraints
        if constraint.name == "uq_repository_indexes_identity"
    )

    assert set(identity.columns.keys()) == {
        "repository",
        "commit_sha",
        "embedding_provider",
        "embedding_model",
        "embedding_dimensions",
        "chunker_version",
    }


def test_repository_chunk_has_cascade_foreign_key() -> None:
    """Link each chunk to a repository index version."""

    column = RepositoryChunk.__table__.c.index_id
    foreign_key = next(iter(column.foreign_keys))

    assert foreign_key.target_fullname == "repository_indexes.id"
    assert foreign_key.ondelete == "CASCADE"
    assert column.nullable is False


def test_repository_chunk_uses_1024_dimensional_vector() -> None:
    """Require the verified embedding dimension in the database model."""

    column = RepositoryChunk.__table__.c.embedding

    assert isinstance(column.type, VECTOR)
    assert column.type.dim is None
    assert column.nullable is False


def test_repository_chunk_metadata_uses_jsonb() -> None:
    """Store structured symbols and headings as PostgreSQL JSONB."""

    columns = RepositoryChunk.__table__.c

    for name in ("symbols", "heading_path", "headings"):
        assert isinstance(columns[name].type, JSONB)
        assert columns[name].nullable is False


def test_repository_chunk_key_is_unique_per_index() -> None:
    """Protect an index from duplicate logical chunks."""

    constraints = [
        constraint
        for constraint in RepositoryChunk.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    key_constraint = next(
        constraint
        for constraint in constraints
        if constraint.name == "uq_repository_chunks_index_key"
    )

    assert set(key_constraint.columns.keys()) == {
        "index_id",
        "chunk_key",
    }
