"""Database models for GitHub history comments and pull-request details."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class RepositoryHistoryCommentRecord(Base):
    """A discussion comment belonging to a stored GitHub issue or PR."""

    __tablename__ = "repository_history_comments"

    __table_args__ = (
        ForeignKeyConstraint(
            ["repository", "issue_number"],
            [
                "repository_history_items.repository",
                "repository_history_items.number",
            ],
            name="fk_history_comment_item",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "comment_id > 0",
            name="ck_history_comment_id",
        ),
        Index(
            "ix_history_comments_issue",
            "repository",
            "issue_number",
        ),
    )

    repository: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )
    comment_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )
    issue_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    author_login: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    body: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    source_url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    source_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    source_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class RepositoryHistoryPullRequestRecord(Base):
    """Additional details for a stored GitHub pull request."""

    __tablename__ = "repository_history_pull_requests"

    __table_args__ = (
        ForeignKeyConstraint(
            ["repository", "number"],
            [
                "repository_history_items.repository",
                "repository_history_items.number",
            ],
            name="fk_history_pull_request_item",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "changed_files >= 0",
            name="ck_history_pull_request_changed_files",
        ),
    )

    repository: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
    )
    number: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )
    merged: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )
    merged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    merge_commit_sha: Mapped[str | None] = mapped_column(
        String(40),
        nullable=True,
    )
    head_sha: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )
    base_sha: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
    )
    changed_files: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    source_item_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
