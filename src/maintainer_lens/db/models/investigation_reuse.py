"""URL arbitration and safe progress history under existing Task ownership."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class InvestigationUrlLookup(Base):
    __tablename__ = "investigation_url_lookups"
    __table_args__ = (
        CheckConstraint("length(intent_key) = 64", name="ck_url_lookup_key"),
        CheckConstraint("issue_number > 0", name="ck_url_lookup_issue"),
        CheckConstraint(
            "(check_token IS NULL) = (check_expires_at IS NULL)",
            name="ck_url_lookup_check_pair",
        ),
        CheckConstraint(
            "jsonb_typeof(knowledge_budget) = 'object'", name="ck_url_lookup_budget"
        ),
    )
    intent_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    knowledge_budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("investigation_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    report_task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("investigation_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    check_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    check_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InvestigationTaskEvent(Base):
    __tablename__ = "investigation_task_events"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="ck_task_events_generation"),
        CheckConstraint(
            "search_round IS NULL OR search_round BETWEEN 1 AND 3",
            name="ck_task_events_round",
        ),
        CheckConstraint(
            "error_payload IS NULL OR jsonb_typeof(error_payload) = 'object'",
            name="ck_task_events_error",
        ),
        Index("ix_task_events_task_order", "task_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("investigation_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    search_round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
