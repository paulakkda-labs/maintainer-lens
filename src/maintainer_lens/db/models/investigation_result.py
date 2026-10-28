"""Durable, versioned results for completed Graph invocations."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base

INVESTIGATION_RESULT_SCHEMA_VERSION = 1


class InvestigationResultRecord(Base):
    """One canonical result artifact; task status remains in InvestigationTask."""

    __tablename__ = "investigation_results"

    __table_args__ = (
        CheckConstraint(
            "schema_version > 0",
            name="ck_investigation_results_schema_version_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(result_payload) = 'object'",
            name="ck_investigation_results_payload_object",
        ),
    )

    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("investigation_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )
    result_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
    )
