"""Store preparation mode, selected Source identity and budget snapshots.

Revision ID: e8b3c7d9a204
Revises: d7e9f2a4b601
Create Date: 2026-10-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8b3c7d9a204"
down_revision: str | Sequence[str] | None = "d7e9f2a4b601"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add legacy-safe preparation metadata and the original standard budget."""
    op.add_column(
        "investigation_tasks",
        sa.Column(
            "auto_prepare_knowledge",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "investigation_tasks",
        sa.Column("prepared_source_index_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    op.add_column(
        "investigation_tasks",
        sa.Column(
            "knowledge_budget",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text(
                '\'{"schema_version": 1,"profile": "standard","profile_version": 1,'
                '"source_max_files": 500,"source_max_bytes": 8388608,'
                '"source_max_chunks": 2000,"history_max_items": 3,'
                '"preparation_timeout_seconds": 900}\'::jsonb'
            ),
        ),
    )
    op.create_check_constraint(
        "ck_investigation_tasks_auto_source_request",
        "investigation_tasks",
        "NOT auto_prepare_knowledge OR source_index_id IS NULL",
    )
    op.create_check_constraint(
        "ck_investigation_tasks_prepared_source_mode",
        "investigation_tasks",
        "auto_prepare_knowledge OR prepared_source_index_id IS NULL",
    )
    op.create_check_constraint(
        "ck_investigation_tasks_knowledge_budget_object",
        "investigation_tasks",
        "jsonb_typeof(knowledge_budget) = 'object'",
    )


def downgrade() -> None:
    """Remove preparation metadata, preserving the original explicit index field."""
    op.drop_constraint(
        "ck_investigation_tasks_knowledge_budget_object",
        "investigation_tasks",
        type_="check",
    )
    op.drop_constraint(
        "ck_investigation_tasks_prepared_source_mode",
        "investigation_tasks",
        type_="check",
    )
    op.drop_constraint(
        "ck_investigation_tasks_auto_source_request",
        "investigation_tasks",
        type_="check",
    )
    op.drop_column("investigation_tasks", "knowledge_budget")
    op.drop_column("investigation_tasks", "prepared_source_index_id")
    op.drop_column("investigation_tasks", "auto_prepare_knowledge")
