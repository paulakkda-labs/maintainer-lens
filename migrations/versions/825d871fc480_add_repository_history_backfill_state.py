"""add_repository_history_backfill_state

Revision ID: 825d871fc480
Revises: dab997a69de5
Create Date: 2026-10-25 22:55:49.287653

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "825d871fc480"
down_revision: str | Sequence[str] | None = "dab997a69de5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repository_history_backfill_states",
        sa.Column(
            "repository",
            sa.String(255),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "next_page",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "per_page",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "next_page > 0",
            name="ck_repository_history_backfill_next_page",
        ),
        sa.CheckConstraint(
            "per_page BETWEEN 1 AND 100",
            name="ck_repository_history_backfill_per_page",
        ),
    )


def downgrade() -> None:
    op.drop_table("repository_history_backfill_states")
