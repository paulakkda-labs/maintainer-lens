"""SQLAlchemy ORM model registry."""

from maintainer_lens.db.base import Base
from maintainer_lens.db.models.investigation import InvestigationTask

__all__ = ["Base", "InvestigationTask"]
