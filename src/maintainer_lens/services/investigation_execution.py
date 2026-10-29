"""Application boundary for one complete, evidence-grounded investigation."""

import asyncio
import math
from datetime import datetime
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from maintainer_lens.agents.investigation.evidence import (
    history_evidence_id,
    to_history_evidence,
    to_source_evidence,
)
from maintainer_lens.agents.investigation.progress import NodeObserver
from maintainer_lens.agents.investigation.query_planning import FollowUpQueryPlanner
from maintainer_lens.agents.investigation.routing import assess_next_step
from maintainer_lens.agents.investigation.state import (
    HistorySearcher,
    InvalidInvestigationResultError,
    InvestigationState,
    SourceSearcher,
)
from maintainer_lens.agents.investigation.tool_node import ToolSearchRunner
from maintainer_lens.agents.investigation_analysis import (
    InvestigationAnalyzer,
)
from maintainer_lens.agents.investigation_graph import build_investigation_graph
from maintainer_lens.schemas.investigation_checkpoint import CheckpointEvidenceContext
from maintainer_lens.schemas.investigation_contracts import (
    InvestigationInput as InvestigationInput,
)
from maintainer_lens.schemas.investigation_contracts import (
    InvestigationResult as InvestigationResult,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)
from maintainer_lens.services.repository_hybrid_search import HybridSearchResult


class InvestigationDeadlineExceeded(TimeoutError):
    """The deadline owned by the complete graph execution expired."""


def validate_investigation_evidence(
    result: InvestigationResult | CheckpointEvidenceContext,
) -> None:
    """Resolve actual analysis citations against all returned evidence."""

    references: dict[str, tuple[str, str]] = {}

    for hit in result.history_evidence:
        # Reuse ingestion provenance checks, including timezone and URL identity.
        to_history_evidence(
            RepositoryHistorySemanticHit(
                **{
                    **hit,
                    "source_updated_at": datetime.fromisoformat(
                        hit["source_updated_at"]
                    ),
                }
            ),
            result.repository,
        )
        if hit["number"] == result.issue_number:
            raise ValueError("The investigated issue cannot be historical evidence")
        evidence_id = history_evidence_id(hit)
        if evidence_id in references:
            raise ValueError("Duplicate returned evidence ID")
        references[evidence_id] = ("history", hit["source_url"])

    for hit in result.source_evidence:
        if result.source_index_id is None:
            raise ValueError("Source evidence requires source_index_id")
        converted = to_source_evidence(
            HybridSearchResult(
                **{
                    key: value
                    for key, value in hit.items()
                    if key not in ("index_id", "source_url")
                },
                index_id=UUID(hit["index_id"]),
            ),
            result.repository,
            result.source_index_id,
        )
        if hit["source_url"] != converted["source_url"]:
            raise ValueError("Source evidence URL does not match its provenance")
        evidence_id = f"source:{hit['index_id']}:{hit['chunk_key']}"
        if evidence_id in references:
            raise ValueError("Duplicate returned evidence ID")
        references[evidence_id] = ("source", hit["source_url"])

    analysis_ids: set[str] = set()
    for item in result.analysis_evidence:
        evidence_id = item["evidence_id"]
        if evidence_id in analysis_ids:
            raise ValueError("Duplicate analysis evidence ID")
        if references.get(evidence_id) != (item["category"], item["source_url"]):
            raise ValueError("Analysis evidence does not resolve to returned evidence")
        analysis_ids.add(evidence_id)

    # Excerpts are the saved analysis input, not reconstructed from final ranking.
    # Historical content can change on a later retrieval of the same object.
    for finding in result.preliminary_analysis.findings:
        if not set(finding.evidence_ids).issubset(analysis_ids):
            raise ValueError("Finding cites evidence outside the last analysis context")


def _validate_result(
    state: InvestigationState,
    request: InvestigationInput,
    max_search_rounds: int,
) -> InvestigationResult:
    """Validate terminal data only; dependency execution is outside this boundary."""

    try:
        for key, expected in request.model_dump(mode="json").items():
            actual = state.get(key) if key == "source_index_id" else state[key]
            if actual != expected:
                raise ValueError("Graph changed the investigation request identity")

        payload = {
            key: state[key]
            for key in InvestigationResult.model_fields
            if key != "source_index_id"
        }
        payload["source_index_id"] = request.source_index_id
        result = InvestigationResult.model_validate(payload, strict=True)

        if result.search_round > max_search_rounds:
            raise ValueError("Graph exceeded max_search_rounds")
        if not result.routing_reason.strip():
            raise ValueError("Graph routing_reason must not be blank")
        if type(state["no_new_evidence"]) is not bool:
            raise ValueError("Invalid graph novelty flag")

        validate_investigation_evidence(result)
        route, _ = assess_next_step(state)
        if (result.next_step == "draft_preliminary_report") != (route == "ready"):
            raise ValueError("Terminal marker disagrees with assess_next_step")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidInvestigationResultError(
            "Invalid terminal investigation result"
        ) from exc


def _prepare_execution(
    request: InvestigationInput,
    *,
    history_searcher: HistorySearcher,
    analyzer: InvestigationAnalyzer,
    timeout_seconds: float,
    source_searcher: SourceSearcher | None = None,
    max_search_rounds: int = 1,
    query_planner: FollowUpQueryPlanner | None = None,
    tool_search_runner: ToolSearchRunner | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    node_observer: NodeObserver | None = None,
) -> tuple[InvestigationInput, CompiledStateGraph, InvestigationState]:
    """Validate options and compile a fresh or checkpoint-capable investigation graph."""

    # Revalidate even model_copy/model_construct inputs before invoking dependencies.
    if not isinstance(request, InvestigationInput):
        raise ValueError("request must be an InvestigationInput")
    request = InvestigationInput.model_validate(
        {**vars(request), **(request.model_extra or {})}
    )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be finite and positive")
    try:
        finite_timeout = math.isfinite(timeout_seconds)
    except OverflowError:
        finite_timeout = False
    if not finite_timeout:
        raise ValueError("timeout_seconds must be finite and positive")

    for name, dependency in (
        ("history_searcher", history_searcher),
        ("analyzer", analyzer),
    ):
        if not callable(dependency):
            raise ValueError(f"{name} must be callable")
    for name, dependency in (
        ("source_searcher", source_searcher),
        ("query_planner", query_planner),
        ("tool_search_runner", tool_search_runner),
        ("node_observer", node_observer),
    ):
        if dependency is not None and not callable(dependency):
            raise ValueError(f"{name} must be callable")
    if source_searcher is not None and request.source_index_id is None:
        raise ValueError("source_index_id is required when source search is enabled")

    graph = build_investigation_graph(
        history_searcher,
        source_searcher=source_searcher,
        analyzer=analyzer,
        max_search_rounds=max_search_rounds,
        query_planner=query_planner,
        tool_search_runner=tool_search_runner,
        **({"checkpointer": checkpointer} if checkpointer is not None else {}),
        **({"node_observer": node_observer} if node_observer is not None else {}),
    )
    initial_state: InvestigationState = {
        "repository": request.repository,
        "issue_number": request.issue_number,
        "issue_title": request.issue_title,
        "issue_body": request.issue_body,
        "response_language": request.response_language,
        "history_evidence": [],
        "source_evidence": [],
    }
    if request.source_index_id is not None:
        initial_state["source_index_id"] = str(request.source_index_id)

    return request, graph, initial_state


async def execute_graph_investigation(
    request: InvestigationInput,
    *,
    history_searcher: HistorySearcher,
    analyzer: InvestigationAnalyzer,
    timeout_seconds: float,
    source_searcher: SourceSearcher | None = None,
    max_search_rounds: int = 1,
    query_planner: FollowUpQueryPlanner | None = None,
    tool_search_runner: ToolSearchRunner | None = None,
) -> InvestigationResult:
    """Run a fresh graph within one deadline, without owning clients or task leases."""

    request, graph, initial_state = _prepare_execution(
        request,
        history_searcher=history_searcher,
        analyzer=analyzer,
        timeout_seconds=timeout_seconds,
        source_searcher=source_searcher,
        max_search_rounds=max_search_rounds,
        query_planner=query_planner,
        tool_search_runner=tool_search_runner,
    )
    timeout_scope = asyncio.timeout(timeout_seconds)
    try:
        async with timeout_scope:
            state = await graph.ainvoke(initial_state)
    except TimeoutError as exc:
        if not timeout_scope.expired():
            raise
        raise InvestigationDeadlineExceeded("Investigation deadline exceeded") from exc

    # A dependency may swallow the deadline cancellation and return normally.
    if timeout_scope.expired():
        raise InvestigationDeadlineExceeded("Investigation deadline exceeded")
    return _validate_result(state, request, max_search_rounds)
