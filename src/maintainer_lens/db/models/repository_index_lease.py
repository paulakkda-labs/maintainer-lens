"""Database model for repository index build leases."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from maintainer_lens.db.base import Base


class RepositoryIndexBuildLease(Base):
    """Exclusive ownership record for one repository index build."""

    __tablename__ = "repository_index_build_leases"

    __table_args__ = (
        CheckConstraint(
            "length(identity_key) = 64",
            name="ck_repository_index_build_leases_key_length",
        ),
    )

    identity_key: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    owner_token: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
