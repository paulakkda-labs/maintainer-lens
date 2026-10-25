"""add_repository_history_details

Revision ID: b00a860b991c
Revises: 825d871fc480
Create Date: 2026-10-25 23:17:16.363649

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b00a860b991c"
down_revision: str | Sequence[str] | None = "825d871fc480"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repository_history_comments",
        sa.Column("repository", sa.String(255), primary_key=True),
        sa.Column("comment_id", sa.BigInteger(), primary_key=True),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("author_login", sa.String(255), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["repository", "issue_number"],
            [
                "repository_history_items.repository",
                "repository_history_items.number",
            ],
            name="fk_history_comment_item",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "comment_id > 0",
            name="ck_history_comment_id",
        ),
    )

    op.create_index(
        "ix_history_comments_issue",
        "repository_history_comments",
        ["repository", "issue_number"],
    )

    op.create_table(
        "repository_history_pull_requests",
        sa.Column("repository", sa.String(255), primary_key=True),
        sa.Column("number", sa.Integer(), primary_key=True),
        sa.Column("merged", sa.Boolean(), nullable=False),
        sa.Column(
            "merged_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "merge_commit_sha",
            sa.String(40),
            nullable=True,
        ),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column("base_sha", sa.String(40), nullable=False),
        sa.Column("changed_files", sa.Integer(), nullable=False),
        sa.Column(
            "source_item_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["repository", "number"],
            [
                "repository_history_items.repository",
                "repository_history_items.number",
            ],
            name="fk_history_pull_request_item",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "changed_files >= 0",
            name="ck_history_pull_request_changed_files",
        ),
    )


def downgrade() -> None:
    op.drop_table("repository_history_pull_requests")
    op.drop_index(
        "ix_history_comments_issue",
        table_name="repository_history_comments",
    )
    op.drop_table("repository_history_comments")
