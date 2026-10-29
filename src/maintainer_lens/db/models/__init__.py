"""SQLAlchemy ORM model registry."""

from maintainer_lens.db.base import Base
from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_checkpoint import (
    InvestigationCheckpoint,
    InvestigationCheckpointWrite,
    InvestigationGraphRun,
)
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.db.models.investigation_reuse import (
    InvestigationTaskEvent,
    InvestigationUrlLookup,
)
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.models.repository_history_backfill import (
    RepositoryHistoryBackfillState,
)
from maintainer_lens.db.models.repository_history_details import (
    RepositoryHistoryCommentRecord,
    RepositoryHistoryPullRequestRecord,
)
from maintainer_lens.db.models.repository_history_embedding import (
    RepositoryHistoryEmbedding,
)
from maintainer_lens.db.models.repository_history_sync import RepositoryHistorySyncState
from maintainer_lens.db.models.repository_index import RepositoryChunk, RepositoryIndex
from maintainer_lens.db.models.repository_index_lease import RepositoryIndexBuildLease

__all__ = [
    "Base",
    "InvestigationTask",
    "InvestigationIssueSnapshot",
    "InvestigationOutboxEvent",
    "InvestigationResultRecord",
    "InvestigationGraphRun",
    "InvestigationCheckpoint",
    "InvestigationCheckpointWrite",
    "InvestigationTaskEvent",
    "InvestigationUrlLookup",
    "RepositoryIndex",
    "RepositoryChunk",
    "RepositoryIndexBuildLease",
    "RepositoryHistoryRecord",
    "RepositoryHistoryCommentRecord",
    "RepositoryHistoryPullRequestRecord",
    "RepositoryHistorySyncState",
    "RepositoryHistoryBackfillState",
    "RepositoryHistoryEmbedding",
]
