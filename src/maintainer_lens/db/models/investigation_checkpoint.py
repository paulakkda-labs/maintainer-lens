"""Application-owned LangGraph persistence, migrated exclusively by Alembic."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class InvestigationGraphRun(Base):
    """Freeze one logical Graph execution under the existing task identity."""

    __tablename__ = "investigation_graph_runs"
    __table_args__ = (
        CheckConstraint("storage_version = 1", name="ck_graph_runs_storage_version"),
        CheckConstraint(
            "jsonb_typeof(request_payload) = 'object'",
            name="ck_graph_runs_request_object",
        ),
        CheckConstraint(
            "jsonb_typeof(execution_payload) = 'object'",
            name="ck_graph_runs_execution_object",
        ),
        CheckConstraint("length(fingerprint) = 64", name="ck_graph_runs_fingerprint"),
    )

    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("investigation_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    storage_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    execution_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InvestigationCheckpoint(Base):
    """A complete typed checkpoint including channel values and parent identity."""

    __tablename__ = "investigation_checkpoints"
    __table_args__ = (
        CheckConstraint("length(checkpoint_id) > 0", name="ck_checkpoints_id_nonempty"),
        CheckConstraint(
            "jsonb_typeof(checkpoint_metadata) = 'object'",
            name="ck_checkpoints_metadata_object",
        ),
    )

    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("investigation_graph_runs.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    checkpoint_ns: Mapped[str] = mapped_column(String(255), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    checkpoint_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InvestigationCheckpointWrite(Base):
    """Replayable node outputs; node_task_id is a LangGraph task, not a DB Task."""

    __tablename__ = "investigation_checkpoint_writes"
    # Writes can precede their snapshot callback; the run owns both tables.
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("investigation_graph_runs.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    checkpoint_ns: Mapped[str] = mapped_column(String(255), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    write_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(255), nullable=False)
    task_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    payload_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
