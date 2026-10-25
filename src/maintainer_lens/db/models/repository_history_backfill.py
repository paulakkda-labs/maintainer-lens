"""Database model for resumable repository history backfill."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class RepositoryHistoryBackfillState(Base):
    """Durable page progress for an initial repository history backfill."""

    __tablename__ = "repository_history_backfill_states"

    __table_args__ = (
        CheckConstraint(
            "next_page > 0",
            name="ck_repository_history_backfill_next_page",
        ),
        CheckConstraint(
            "per_page BETWEEN 1 AND 100",
            name="ck_repository_history_backfill_per_page",
        ),
    )

    repository: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )

    next_page: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    per_page: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
