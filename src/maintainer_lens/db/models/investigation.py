from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class InvestigationTask(Base):
    """Persist the lifecycle state of an investigation task."""

    __tablename__ = "investigation_tasks"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )
    repository: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    issue_number: Mapped[int] = mapped_column(
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        server_default="pending",
        nullable=False,
    )
    claim_token: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "issue_number > 0",
            name="ck_investigation_tasks_issue_number_positive",
        ),
    )
