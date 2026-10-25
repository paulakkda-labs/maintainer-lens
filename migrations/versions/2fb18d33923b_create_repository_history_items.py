"""create_repository_history_items

Revision ID: 2fb18d33923b
Revises: 2d677b9f2e0b
Create Date: 2026-10-25 22:11:14.858016

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2fb18d33923b"
down_revision: str | Sequence[str] | None = "2d677b9f2e0b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create durable repository history storage."""

    op.create_table(
        "repository_history_items",
        sa.Column(
            "repository",
            sa.String(255),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "number",
            sa.Integer(),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "source_created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "source_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "number > 0",
            name="ck_repository_history_items_number",
        ),
        sa.CheckConstraint(
            "kind IN ('issue', 'pull_request')",
            name="ck_repository_history_items_kind",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'closed')",
            name="ck_repository_history_items_state",
        ),
    )

    op.create_index(
        "ix_repository_history_items_repo_updated",
        "repository_history_items",
        ["repository", "source_updated_at"],
    )


def downgrade() -> None:
    """Remove repository history storage."""

    op.drop_index(
        "ix_repository_history_items_repo_updated",
        table_name="repository_history_items",
    )

    op.drop_table("repository_history_items")
