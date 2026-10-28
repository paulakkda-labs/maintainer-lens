"""Create versioned investigation result artifacts.

Revision ID: c4b2a6d8e901
Revises: 789b1caf1332
Create Date: 2026-10-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4b2a6d8e901"
down_revision: str | Sequence[str] | None = "789b1caf1332"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add one durable result row per investigation task."""
    op.create_table(
        "investigation_results",
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column(
            "schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("result_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_investigation_results_schema_version_positive",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result_payload) = 'object'",
            name="ck_investigation_results_payload_object",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["investigation_tasks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    """Remove the result table; task and snapshot tables are unchanged."""
    op.drop_table("investigation_results")
