"""Database model for repository history synchronization progress."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class RepositoryHistorySyncState(Base):
    """Durable progress of repository issue and pull-request synchronization."""

    __tablename__ = "repository_history_sync_states"

    __table_args__ = (
        CheckConstraint(
            "last_stop_reason IN ('exhausted', 'item_limit', 'page_limit')",
            name="ck_repository_history_sync_stop_reason",
        ),
        CheckConstraint(
            "last_pages_fetched > 0",
            name="ck_repository_history_sync_pages",
        ),
        CheckConstraint(
            "last_item_count >= 0",
            name="ck_repository_history_sync_items",
        ),
    )

    repository: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )

    completed_through: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    last_stop_reason: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )

    last_pages_fetched: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    last_item_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
