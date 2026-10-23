"""add repository vector indexes

Revision ID: b25976191177
Revises: 2abd86056cfc
Create Date: 2026-10-23 22:45:17.217217

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "b25976191177"
down_revision: str | Sequence[str] | None = "2abd86056cfc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create versioned repository indexes and vector storage."""

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "repository_indexes",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("tree_sha", sa.String(40), nullable=False),
        sa.Column("embedding_provider", sa.String(128), nullable=False),
        sa.Column("embedding_model", sa.String(255), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("chunker_version", sa.String(32), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="building",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "repository",
            "commit_sha",
            "embedding_provider",
            "embedding_model",
            "embedding_dimensions",
            "chunker_version",
            name="uq_repository_indexes_identity",
        ),
        sa.CheckConstraint(
            "embedding_dimensions = 1024",
            name="ck_repository_indexes_dimensions",
        ),
        sa.CheckConstraint(
            "status IN ('building', 'ready', 'failed')",
            name="ck_repository_indexes_status",
        ),
        sa.CheckConstraint(
            "length(commit_sha) = 40 AND length(tree_sha) = 40",
            name="ck_repository_indexes_sha_length",
        ),
    )

    op.create_index(
        "ix_repository_indexes_repository_status",
        "repository_indexes",
        ["repository", "status"],
    )

    op.create_table(
        "repository_chunks",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "index_id",
            sa.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("chunk_key", sa.String(64), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("blob_sha", sa.String(40), nullable=False),
        sa.Column("language", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column(
            "symbols",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "heading_path",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "headings",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", VECTOR(1024), nullable=False),
        sa.ForeignKeyConstraint(
            ["index_id"],
            ["repository_indexes.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "index_id",
            "chunk_key",
            name="uq_repository_chunks_index_key",
        ),
        sa.CheckConstraint(
            "start_line >= 1 AND end_line >= start_line",
            name="ck_repository_chunks_line_range",
        ),
        sa.CheckConstraint(
            "length(chunk_key) = 64",
            name="ck_repository_chunks_key_length",
        ),
    )

    op.create_index(
        "ix_repository_chunks_index_id",
        "repository_chunks",
        ["index_id"],
    )


def downgrade() -> None:
    """Remove repository index tables without dropping pgvector."""

    op.drop_index(
        "ix_repository_chunks_index_id",
        table_name="repository_chunks",
    )

    op.drop_table("repository_chunks")

    op.drop_index(
        "ix_repository_indexes_repository_status",
        table_name="repository_indexes",
    )

    op.drop_table("repository_indexes")
