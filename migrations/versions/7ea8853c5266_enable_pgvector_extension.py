"""enable pgvector extension

Revision ID: 7ea8853c5266
Revises:
Create Date: 2026-10-14 01:48:46.473921

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7ea8853c5266"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable the pgvector extension if it is not already installed."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    """Preserve the extension to avoid removing dependent database objects."""
    pass
