"""Add URL arbitration, delivery generations and safe progress events.

Revision ID: 0ab5e7f9c306
Revises: f9a4d6e8b205
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0ab5e7f9c306"
down_revision: str | Sequence[str] | None = "f9a4d6e8b205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investigation_tasks",
        sa.Column(
            "execution_generation",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "investigation_tasks",
        sa.Column(
            "failure_retryable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "investigation_tasks",
        sa.Column(
            "freshness_payload", postgresql.JSONB(none_as_null=True), nullable=True
        ),
    )
    op.create_check_constraint(
        "ck_tasks_execution_generation",
        "investigation_tasks",
        "execution_generation >= 0",
    )
    op.create_check_constraint(
        "ck_tasks_freshness_object",
        "investigation_tasks",
        "freshness_payload IS NULL OR jsonb_typeof(freshness_payload) = 'object'",
    )
    op.add_column(
        "investigation_outbox_events",
        sa.Column(
            "generation", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_outbox_generation", "investigation_outbox_events", "generation >= 0"
    )
    op.drop_constraint(
        "uq_investigation_outbox_task_event",
        "investigation_outbox_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_investigation_outbox_task_event",
        "investigation_outbox_events",
        ["task_id", "event_type", "generation"],
    )
    op.create_table(
        "investigation_url_lookups",
        sa.Column("intent_key", sa.String(64), primary_key=True),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("knowledge_budget", postgresql.JSONB(), nullable=False),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("investigation_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("check_token", sa.String(64), nullable=True),
        sa.Column(
            "report_task_id",
            sa.String(64),
            sa.ForeignKey("investigation_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("check_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(intent_key) = 64", name="ck_url_lookup_key"),
        sa.CheckConstraint("issue_number > 0", name="ck_url_lookup_issue"),
        sa.CheckConstraint(
            "(check_token IS NULL) = (check_expires_at IS NULL)",
            name="ck_url_lookup_check_pair",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(knowledge_budget) = 'object'", name="ck_url_lookup_budget"
        ),
    )
    op.create_table(
        "investigation_task_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("investigation_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("search_round", sa.Integer(), nullable=True),
        sa.Column("error_payload", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("generation >= 0", name="ck_task_events_generation"),
        sa.CheckConstraint(
            "search_round IS NULL OR search_round BETWEEN 1 AND 3",
            name="ck_task_events_round",
        ),
        sa.CheckConstraint(
            "error_payload IS NULL OR jsonb_typeof(error_payload) = 'object'",
            name="ck_task_events_error",
        ),
    )
    op.create_index(
        "ix_task_events_task_order", "investigation_task_events", ["task_id", "id"]
    )


def downgrade() -> None:
    # Multiple generations cannot fit the previous uniqueness contract. Refuse
    # before dropping anything; never discard recovery events to force downgrade.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM investigation_tasks WHERE execution_generation > 0) OR EXISTS (SELECT 1 FROM investigation_outbox_events WHERE generation > 0) THEN RAISE EXCEPTION 'Delivery generations prevent downgrade'; END IF; END $$"
    )
    op.drop_index("ix_task_events_task_order", table_name="investigation_task_events")
    op.drop_table("investigation_task_events")
    op.drop_table("investigation_url_lookups")
    op.drop_constraint(
        "uq_investigation_outbox_task_event",
        "investigation_outbox_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_investigation_outbox_task_event",
        "investigation_outbox_events",
        ["task_id", "event_type"],
    )
    op.drop_constraint(
        "ck_outbox_generation", "investigation_outbox_events", type_="check"
    )
    op.drop_column("investigation_outbox_events", "generation")
    op.drop_constraint(
        "ck_tasks_freshness_object", "investigation_tasks", type_="check"
    )
    op.drop_constraint(
        "ck_tasks_execution_generation", "investigation_tasks", type_="check"
    )
    for name in ("freshness_payload", "failure_retryable", "execution_generation"):
        op.drop_column("investigation_tasks", name)
