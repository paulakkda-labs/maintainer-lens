"""SQLAlchemy models for versioned repository vector indexes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class RepositoryIndex(Base):
    """A repository revision indexed with one embedding configuration."""

    __tablename__ = "repository_indexes"

    __table_args__ = (
        UniqueConstraint(
            "repository",
            "commit_sha",
            "embedding_provider",
            "embedding_model",
            "embedding_dimensions",
            "chunker_version",
            name="uq_repository_indexes_identity",
        ),
        CheckConstraint(
            "embedding_dimensions > 0",
            name="ck_repository_indexes_dimensions",
        ),
        CheckConstraint(
            "status IN ('building', 'ready', 'failed')",
            name="ck_repository_indexes_status",
        ),
        CheckConstraint(
            "length(commit_sha) = 40 AND length(tree_sha) = 40",
            name="ck_repository_indexes_sha_length",
        ),
        Index(
            "ix_repository_indexes_repository_status",
            "repository",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    repository: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    commit_sha: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    tree_sha: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    embedding_provider: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    embedding_model: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    embedding_dimensions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    chunker_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="building",
        server_default="building",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class RepositoryChunk(Base):
    """A source chunk and its embedding for one repository index."""

    __tablename__ = "repository_chunks"

    __table_args__ = (
        UniqueConstraint(
            "index_id",
            "chunk_key",
            name="uq_repository_chunks_index_key",
        ),
        CheckConstraint(
            "start_line >= 1 AND end_line >= start_line",
            name="ck_repository_chunks_line_range",
        ),
        CheckConstraint(
            "length(chunk_key) = 64",
            name="ck_repository_chunks_key_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    index_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "repository_indexes.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    chunk_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    file_path: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    blob_sha: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )

    language: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )

    symbol: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    symbols: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )

    heading_path: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )

    headings: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )

    start_line: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    end_line: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    embedding: Mapped[list[float]] = mapped_column(
        VECTOR(),
        nullable=False,
    )
