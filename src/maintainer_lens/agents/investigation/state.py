"""State and dependency contracts for the investigation workflow."""

from collections.abc import Awaitable, Callable
from typing import Literal, NotRequired, TypedDict
from uuid import UUID

from maintainer_lens.agents.investigation_analysis import (
    AnalysisResultState,
    ResponseLanguage,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)
from maintainer_lens.services.repository_hybrid_search import (
    HybridSearchResult,
)


class HistoryEvidence(TypedDict):
    """A serializable historical evidence reference."""

    repository: str
    number: int
    source_type: Literal["item", "comment"]
    comment_id: int | None
    title: str
    content: str
    source_url: str
    source_updated_at: str
    distance: float


class SourceEvidence(TypedDict):
    """Source evidence pinned to a repository commit."""

    index_id: str
    chunk_key: str
    repository: str
    commit_sha: str
    blob_sha: str
    file_path: str
    language: str
    kind: str
    symbol: str | None
    start_line: int
    end_line: int
    content: str
    source_url: str
    rrf_score: float
    vector_rank: int | None
    keyword_rank: int | None
    cosine_distance: float | None
    matched_in: str | None


type InvestigationNextStep = Literal[
    "draft_preliminary_report",
    "needs_more_evidence",
]

type AnalysisRoute = Literal["ready", "follow_up", "retry"]


class InvestigationState(TypedDict):
    """Shared state for a bounded issue investigation."""

    repository: str
    issue_number: int
    issue_title: str
    issue_body: str | None

    source_index_id: NotRequired[str]
    response_language: NotRequired[ResponseLanguage]

    history_query: NotRequired[str]
    history_evidence: NotRequired[list[HistoryEvidence]]
    source_evidence: NotRequired[list[SourceEvidence]]
    preliminary_analysis: NotRequired[AnalysisResultState]

    search_round: NotRequired[int]
    searched_queries: NotRequired[list[str]]
    new_history_evidence: NotRequired[int]
    new_source_evidence: NotRequired[int]
    no_new_evidence: NotRequired[bool]

    next_step: NotRequired[InvestigationNextStep]
    routing_reason: NotRequired[str]


type HistorySearcher = Callable[
    [str, str],
    Awaitable[tuple[RepositoryHistorySemanticHit, ...]],
]

type SourceSearcher = Callable[
    [str, UUID, str],
    Awaitable[list[HybridSearchResult]],
]
