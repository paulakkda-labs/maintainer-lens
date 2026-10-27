"""Adapt a bounded tool-calling step to the investigation graph."""

from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx

from maintainer_lens.agents.investigation.evidence import (
    merge_history_evidence,
    merge_source_evidence,
)
from maintainer_lens.agents.investigation.state import (
    HistorySearcher,
    InvestigationState,
    SourceSearcher,
)
from maintainer_lens.agents.investigation.tool_calling import (
    SearchToolStepResult,
    run_search_tool_step,
)
from maintainer_lens.agents.investigation_analysis import ResponseLanguage

type ToolSearchRunner = Callable[
    [InvestigationState],
    Awaitable[SearchToolStepResult],
]


def create_tool_search_runner(
    http_client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model_name: str,
    history_searcher: HistorySearcher,
    source_searcher: SourceSearcher | None = None,
) -> ToolSearchRunner:
    """Bind the LLM and retrieval dependencies for one tool step."""

    async def run(state: InvestigationState) -> SearchToolStepResult:
        analysis = state["preliminary_analysis"]

        candidates = [
            *analysis["unanswered_questions"],
            *analysis["hypotheses"],
        ]

        question = next(
            (item for item in candidates if item.strip()),
            analysis["summary"],
        )

        source_index_id: UUID | None = None

        if source_searcher is not None:
            source_index_id = UUID(state["source_index_id"])

        language: ResponseLanguage = state["response_language"]

        return await run_search_tool_step(
            http_client,
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
            repository=state["repository"],
            question=question,
            response_language=language,
            history_searcher=history_searcher,
            source_searcher=source_searcher,
            source_index_id=source_index_id,
            searched_queries=state["searched_queries"],
            excluded_issue_number=state["issue_number"],
        )

    return run


def make_tool_search_node(tool_search_runner: ToolSearchRunner):
    """Create a node that merges one model-selected retrieval result."""

    async def execute_search_tool(state: InvestigationState) -> dict:
        result = await tool_search_runner(state)

        if result["skipped_duplicate"]:
            return {
                "tool_search_skipped": True,
                "tool_stop_reason": ("The LLM selected a previously searched query."),
                "last_tool_name": result["tool_name"],
                "last_tool_call_id": result["tool_call_id"],
            }

        if result["tool_name"] == "search_history":
            if result["source_evidence"]:
                raise ValueError("History tool returned unexpected source evidence")
        elif result["tool_name"] == "search_source":
            if result["history_evidence"]:
                raise ValueError("Source tool returned unexpected history evidence")
        else:
            raise ValueError("Unknown tool result type")

        previous_queries = {
            " ".join(query.split()).casefold() for query in state["searched_queries"]
        }
        normalized_query = " ".join(result["query"].split()).casefold()

        if normalized_query in previous_queries:
            return {
                "tool_search_skipped": True,
                "tool_stop_reason": (
                    "The tool result repeated a previously searched query."
                ),
                "last_tool_name": result["tool_name"],
                "last_tool_call_id": result["tool_call_id"],
            }

        history, new_history_count = merge_history_evidence(
            state.get("history_evidence", []),
            [
                hit
                for hit in result["history_evidence"]
                if hit["number"] != state["issue_number"]
            ],
        )

        source, new_source_count = merge_source_evidence(
            state.get("source_evidence", []),
            result["source_evidence"],
        )

        no_new_evidence = new_history_count == 0 and new_source_count == 0

        return {
            "history_evidence": history,
            "source_evidence": source,
            "new_history_evidence": new_history_count,
            "new_source_evidence": new_source_count,
            "no_new_evidence": no_new_evidence,
            "history_query": result["query"],
            "searched_queries": [
                *state["searched_queries"],
                result["query"],
            ],
            "search_round": state["search_round"] + 1,
            "tool_search_skipped": False,
            "tool_stop_reason": "",
            "last_tool_name": result["tool_name"],
            "last_tool_call_id": result["tool_call_id"],
            "last_tool_feedback": result["model_response"],
        }

    return execute_search_tool


def route_after_tool_search(
    state: InvestigationState,
) -> str:
    """Continue analysis only when the tool found new evidence."""

    if state.get("tool_search_skipped", False):
        return "stop"

    if state.get("no_new_evidence", False):
        return "stop"

    return "analyze"
