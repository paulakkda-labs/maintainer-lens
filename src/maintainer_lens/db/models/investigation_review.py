"""Durable review requests and immutable, request-scoped decisions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class InvestigationReview(Base):
    __tablename__ = "investigation_reviews"
    __table_args__ = (
        UniqueConstraint("task_id", "generation", name="uq_review_task_generation"),
        CheckConstraint("generation >= 0", name="ck_review_generation"),
        CheckConstraint(
            "jsonb_typeof(context_payload) = 'object'", name="ck_review_context"
        ),
        CheckConstraint(
            "decision_payload IS NULL OR jsonb_typeof(decision_payload) = 'object'",
            name="ck_review_decision",
        ),
        CheckConstraint(
            "(decision_payload IS NULL) = (decided_at IS NULL)",
            name="ck_review_decided_pair",
        ),
        CheckConstraint(
            "target_generation IS NULL OR (decision_payload IS NOT NULL AND target_generation > generation)",
            name="ck_review_target",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("investigation_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    context_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decision_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    target_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
