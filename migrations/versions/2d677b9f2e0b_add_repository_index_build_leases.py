"""add repository index build leases

Revision ID: 2d677b9f2e0b
Revises: 7941212fb0e1
Create Date: 2026-10-24 01:03:28.128251

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2d677b9f2e0b"
down_revision: str | Sequence[str] | None = "7941212fb0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create repository index build leases."""

    op.create_table(
        "repository_index_build_leases",
        sa.Column(
            "identity_key",
            sa.String(64),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "owner_token",
            sa.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "length(identity_key) = 64",
            name="ck_repository_index_build_leases_key_length",
        ),
    )


def downgrade() -> None:
    """Remove repository index build leases."""

    op.drop_table("repository_index_build_leases")
