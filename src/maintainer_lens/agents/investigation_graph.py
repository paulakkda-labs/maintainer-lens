"""Build the LangGraph investigation workflow."""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from maintainer_lens.agents.investigation.analysis_node import (
    make_analysis_node,
)
from maintainer_lens.agents.investigation.retrieval import (
    create_history_searcher,
    create_source_searcher,
    make_history_search_node,
    make_source_search_node,
)
from maintainer_lens.agents.investigation.routing import (
    make_analysis_router,
    make_prepare_query_node,
    mark_more_evidence_needed,
    mark_ready_for_report,
    prepare_follow_up_query,
)
from maintainer_lens.agents.investigation.state import (
    HistorySearcher,
    InvestigationState,
    SourceSearcher,
)
from maintainer_lens.agents.investigation_analysis import (
    InvestigationAnalyzer,
)

__all__ = [
    "build_investigation_graph",
    "create_history_searcher",
    "create_source_searcher",
]


def build_investigation_graph(
    history_searcher: HistorySearcher,
    *,
    source_searcher: SourceSearcher | None = None,
    analyzer: InvestigationAnalyzer | None = None,
    max_search_rounds: int = 1,
) -> CompiledStateGraph:
    """Build the bounded retrieval and analysis workflow."""

    if type(max_search_rounds) is not int or not 1 <= max_search_rounds <= 3:
        raise ValueError("max_search_rounds must be between 1 and 3")

    builder = StateGraph(InvestigationState)

    builder.add_node(
        "prepare_query",
        make_prepare_query_node(require_source_index=source_searcher is not None),
    )
    builder.add_node(
        "search_history",
        make_history_search_node(history_searcher),
    )

    builder.add_edge(START, "prepare_query")
    builder.add_edge("prepare_query", "search_history")

    last_search_node = "search_history"

    if source_searcher is not None:
        builder.add_node(
            "search_source",
            make_source_search_node(source_searcher),
        )
        builder.add_edge("search_history", "search_source")
        last_search_node = "search_source"

    if analyzer is not None:
        builder.add_node(
            "analyze_evidence",
            make_analysis_node(analyzer),
        )
        builder.add_edge(last_search_node, "analyze_evidence")

        builder.add_node(
            "mark_ready_for_report",
            mark_ready_for_report,
        )
        builder.add_node(
            "mark_more_evidence_needed",
            mark_more_evidence_needed,
        )

        path_map = {
            "ready": "mark_ready_for_report",
            "follow_up": "mark_more_evidence_needed",
        }

        if max_search_rounds > 1:
            builder.add_node(
                "prepare_follow_up_query",
                prepare_follow_up_query,
            )
            builder.add_edge(
                "prepare_follow_up_query",
                "search_history",
            )
            path_map["retry"] = "prepare_follow_up_query"

        builder.add_conditional_edges(
            "analyze_evidence",
            make_analysis_router(max_search_rounds),
            path_map,
        )

        builder.add_edge("mark_ready_for_report", END)
        builder.add_edge("mark_more_evidence_needed", END)
    else:
        builder.add_edge(last_search_node, END)

    return builder.compile()
