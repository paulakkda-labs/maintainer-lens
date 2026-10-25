"""Vector embeddings for historical GitHub issues, PRs, and comments."""

from datetime import datetime

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class RepositoryHistoryEmbedding(Base):
    """One embedded historical evidence record for a model configuration."""

    __tablename__ = "repository_history_embeddings"

    __table_args__ = (
        ForeignKeyConstraint(
            ["repository", "number"],
            [
                "repository_history_items.repository",
                "repository_history_items.number",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["repository", "comment_id"],
            [
                "repository_history_comments.repository",
                "repository_history_comments.comment_id",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "embedding_dimensions > 0",
            name="ck_history_embedding_dimensions",
        ),
        CheckConstraint(
            "source_type IN ('item', 'comment')",
            name="ck_history_embedding_source_type",
        ),
        CheckConstraint(
            "(source_type = 'item' AND comment_id IS NULL) OR "
            "(source_type = 'comment' AND comment_id IS NOT NULL)",
            name="ck_history_embedding_comment_identity",
        ),
    )

    repository: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )
    source_key: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    embedding_provider: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    embedding_model: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )
    embedding_dimensions: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    source_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )
    number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    comment_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    source_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    content_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    embedding: Mapped[list[float]] = mapped_column(
        VECTOR(),
        nullable=False,
    )
