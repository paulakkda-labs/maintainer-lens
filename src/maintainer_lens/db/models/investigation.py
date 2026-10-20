from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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


class InvestigationIssueSnapshot(Base):
    __tablename__ = "investigation_issue_snapshots"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("investigation_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)

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
        server_default=func.now(),
        nullable=False,
    )


class InvestigationOutboxEvent(Base):
    """A durable event awaiting publication to RabbitMQ."""

    __tablename__ = "investigation_outbox_events"

    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "event_type",
            name="uq_investigation_outbox_task_event",
        ),
        Index(
            "ix_investigation_outbox_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    task_id: Mapped[str] = mapped_column(
        ForeignKey("investigation_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )

    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )

    publish_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    delivery_token: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    delivery_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
