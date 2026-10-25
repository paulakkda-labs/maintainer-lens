"""Database model for repository issue and pull-request history."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class RepositoryHistoryRecord(Base):
    """A GitHub issue or pull request stored as reusable repository knowledge."""

    __tablename__ = "repository_history_items"

    __table_args__ = (
        CheckConstraint(
            "number > 0",
            name="ck_repository_history_items_number",
        ),
        CheckConstraint(
            "kind IN ('issue', 'pull_request')",
            name="ck_repository_history_items_kind",
        ),
        CheckConstraint(
            "state IN ('open', 'closed')",
            name="ck_repository_history_items_state",
        ),
        Index(
            "ix_repository_history_items_repo_updated",
            "repository",
            "source_updated_at",
        ),
    )

    repository: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )

    number: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    body: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )

    source_url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    source_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    source_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
