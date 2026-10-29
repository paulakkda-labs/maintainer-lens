"""Persist the task's immutable report language; existing production tasks are Chinese.

Revision ID: 3cd7a9b1e528
Revises: 1bc6f8a0d417
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3cd7a9b1e528"
down_revision: str | Sequence[str] | None = "1bc6f8a0d417"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investigation_tasks",
        sa.Column(
            "response_language",
            sa.String(2),
            nullable=False,
            server_default=sa.text("'zh'"),
        ),
    )
    op.create_check_constraint(
        "ck_tasks_response_language",
        "investigation_tasks",
        "response_language IN ('zh', 'en')",
    )
    # Preserve historical Chinese tasks, then make future INSERTs English.
    op.alter_column(
        "investigation_tasks", "response_language", server_default=sa.text("'en'")
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM investigation_tasks WHERE response_language <> 'zh') THEN RAISE EXCEPTION 'Non-Chinese investigation history prevents downgrade'; END IF; END $$"
    )
    op.drop_constraint(
        "ck_tasks_response_language", "investigation_tasks", type_="check"
    )
    op.drop_column("investigation_tasks", "response_language")
