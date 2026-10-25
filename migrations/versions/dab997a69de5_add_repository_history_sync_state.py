"""add_repository_history_sync_state

Revision ID: dab997a69de5
Revises: 2fb18d33923b
Create Date: 2026-10-25 22:34:28.851745

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dab997a69de5"
down_revision: str | Sequence[str] | None = "2fb18d33923b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repository_history_sync_states",
        sa.Column("repository", sa.String(255), primary_key=True),
        sa.Column(
            "completed_through",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_stop_reason",
            sa.String(16),
            nullable=False,
        ),
        sa.Column(
            "last_pages_fetched",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "last_item_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_stop_reason IN ('exhausted', 'item_limit', 'page_limit')",
            name="ck_repository_history_sync_stop_reason",
        ),
        sa.CheckConstraint(
            "last_pages_fetched > 0",
            name="ck_repository_history_sync_pages",
        ),
        sa.CheckConstraint(
            "last_item_count >= 0",
            name="ck_repository_history_sync_items",
        ),
    )


def downgrade() -> None:
    op.drop_table("repository_history_sync_states")
