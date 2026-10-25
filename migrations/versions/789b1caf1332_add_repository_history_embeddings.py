"""add_repository_history_embeddings

Revision ID: 789b1caf1332
Revises: b00a860b991c
Create Date: 2026-10-26 00:25:27.798942

"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "789b1caf1332"
down_revision: str | Sequence[str] | None = "b00a860b991c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repository_history_embeddings",
        sa.Column("repository", sa.String(255), primary_key=True),
        sa.Column("source_key", sa.String(64), primary_key=True),
        sa.Column("embedding_provider", sa.String(128), primary_key=True),
        sa.Column("embedding_model", sa.String(255), primary_key=True),
        sa.Column("embedding_dimensions", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("comment_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "source_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.VECTOR(), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository", "number"],
            [
                "repository_history_items.repository",
                "repository_history_items.number",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository", "comment_id"],
            [
                "repository_history_comments.repository",
                "repository_history_comments.comment_id",
            ],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "embedding_dimensions > 0",
            name="ck_history_embedding_dimensions",
        ),
        sa.CheckConstraint(
            "source_type IN ('item', 'comment')",
            name="ck_history_embedding_source_type",
        ),
        sa.CheckConstraint(
            "(source_type = 'item' AND comment_id IS NULL) OR "
            "(source_type = 'comment' AND comment_id IS NOT NULL)",
            name="ck_history_embedding_comment_identity",
        ),
    )


def downgrade() -> None:
    op.drop_table("repository_history_embeddings")
