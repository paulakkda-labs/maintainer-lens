"""Persist frozen Graph runs, checkpoints and pending node writes.

Revision ID: f9a4d6e8b205
Revises: e8b3c7d9a204
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f9a4d6e8b205"
down_revision: str | Sequence[str] | None = "e8b3c7d9a204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add persistence without changing the existing task lifecycle or old rows."""
    op.create_table(
        "investigation_graph_runs",
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("storage_version", sa.Integer(), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("execution_payload", postgresql.JSONB(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("task_id"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["investigation_tasks.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("storage_version = 1", name="ck_graph_runs_storage_version"),
        sa.CheckConstraint(
            "jsonb_typeof(request_payload) = 'object'",
            name="ck_graph_runs_request_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(execution_payload) = 'object'",
            name="ck_graph_runs_execution_object",
        ),
        sa.CheckConstraint(
            "length(fingerprint) = 64", name="ck_graph_runs_fingerprint"
        ),
    )
    op.create_table(
        "investigation_checkpoints",
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("checkpoint_ns", sa.String(255), nullable=False),
        sa.Column("checkpoint_id", sa.String(64), nullable=False),
        sa.Column("parent_checkpoint_id", sa.String(64), nullable=True),
        sa.Column("payload_type", sa.String(32), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("checkpoint_metadata", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("task_id", "checkpoint_ns", "checkpoint_id"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["investigation_graph_runs.task_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "length(checkpoint_id) > 0", name="ck_checkpoints_id_nonempty"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(checkpoint_metadata) = 'object'",
            name="ck_checkpoints_metadata_object",
        ),
    )
    op.create_table(
        "investigation_checkpoint_writes",
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("checkpoint_ns", sa.String(255), nullable=False),
        sa.Column("checkpoint_id", sa.String(64), nullable=False),
        sa.Column("node_task_id", sa.String(128), nullable=False),
        sa.Column("write_index", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(255), nullable=False),
        sa.Column("task_path", sa.String(1024), nullable=False),
        sa.Column("payload_type", sa.String(32), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint(
            "task_id", "checkpoint_ns", "checkpoint_id", "node_task_id", "write_index"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["investigation_graph_runs.task_id"], ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    """Remove only Graph progress storage; preserve business tasks and results."""
    op.drop_table("investigation_checkpoint_writes")
    op.drop_table("investigation_checkpoints")
    op.drop_table("investigation_graph_runs")
