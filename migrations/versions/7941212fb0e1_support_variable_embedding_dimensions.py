"""support variable embedding dimensions

Revision ID: 7941212fb0e1
Revises: b25976191177
Create Date: 2026-10-23 22:58:46.961080

"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import VECTOR

# revision identifiers, used by Alembic.
revision: str = "7941212fb0e1"
down_revision: str | Sequence[str] | None = "b25976191177"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow repository indexes to use different vector dimensions."""

    op.drop_constraint(
        "ck_repository_indexes_dimensions",
        "repository_indexes",
        type_="check",
    )

    op.create_check_constraint(
        "ck_repository_indexes_dimensions",
        "repository_indexes",
        "embedding_dimensions > 0",
    )

    op.alter_column(
        "repository_chunks",
        "embedding",
        existing_type=VECTOR(1024),
        type_=VECTOR(),
        existing_nullable=False,
        postgresql_using="embedding::vector",
    )


def downgrade() -> None:
    """Restore the original 1024-dimensional vector schema."""

    op.alter_column(
        "repository_chunks",
        "embedding",
        existing_type=VECTOR(),
        type_=VECTOR(1024),
        existing_nullable=False,
        postgresql_using="embedding::vector(1024)",
    )

    op.drop_constraint(
        "ck_repository_indexes_dimensions",
        "repository_indexes",
        type_="check",
    )

    op.create_check_constraint(
        "ck_repository_indexes_dimensions",
        "repository_indexes",
        "embedding_dimensions = 1024",
    )
