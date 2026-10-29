"""Add on-demand reviews and separate recovery counters.

Revision ID: 1bc6f8a0d417
Revises: 0ab5e7f9c306
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1bc6f8a0d417"
down_revision: str | Sequence[str] | None = "0ab5e7f9c306"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("user_recovery_count", "budget_resume_count"):
        op.add_column(
            "investigation_tasks",
            sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False),
        )
        op.create_check_constraint(
            f"ck_tasks_{name}", "investigation_tasks", f"{name} BETWEEN 0 AND 3"
        )
    # Before this revision, only user recovery incremented the delivery generation.
    op.execute(
        "UPDATE investigation_tasks SET user_recovery_count = execution_generation"
    )
    op.create_table(
        "investigation_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("investigation_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("context_payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "decision_payload", postgresql.JSONB(none_as_null=True), nullable=True
        ),
        sa.Column("target_generation", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", "generation", name="uq_review_task_generation"),
        sa.CheckConstraint("generation >= 0", name="ck_review_generation"),
        sa.CheckConstraint(
            "jsonb_typeof(context_payload) = 'object'", name="ck_review_context"
        ),
        sa.CheckConstraint(
            "decision_payload IS NULL OR jsonb_typeof(decision_payload) = 'object'",
            name="ck_review_decision",
        ),
        sa.CheckConstraint(
            "(decision_payload IS NULL) = (decided_at IS NULL)",
            name="ck_review_decided_pair",
        ),
        sa.CheckConstraint(
            "target_generation IS NULL OR (decision_payload IS NOT NULL AND target_generation > generation)",
            name="ck_review_target",
        ),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM investigation_reviews) OR EXISTS (SELECT 1 FROM investigation_tasks WHERE status = 'waiting_review' OR execution_generation <> user_recovery_count) THEN RAISE EXCEPTION 'Human review history prevents downgrade'; END IF; END $$"
    )
    op.drop_table("investigation_reviews")
    for name in ("budget_resume_count", "user_recovery_count"):
        op.drop_constraint(f"ck_tasks_{name}", "investigation_tasks", type_="check")
        op.drop_column("investigation_tasks", name)
