"""Preserve the explicitly requested Source index on investigation tasks.

Revision ID: d7e9f2a4b601
Revises: c4b2a6d8e901
Create Date: 2026-10-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e9f2a4b601"
down_revision: str | Sequence[str] | None = "c4b2a6d8e901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add optional request identity without coupling index/task lifetimes."""
    op.add_column(
        "investigation_tasks",
        sa.Column("source_index_id", sa.Uuid(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    """Remove the request field; existing task lifecycle columns are unchanged."""
    op.drop_column("investigation_tasks", "source_index_id")
