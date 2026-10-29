"""Build the LangGraph investigation workflow."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from maintainer_lens.agents.investigation.analysis_node import (
    make_analysis_node,
)
from maintainer_lens.agents.investigation.query_planning import (
    FollowUpQueryPlanner,
    apply_planned_query,
    make_query_planning_node,
    route_after_planning,
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
from maintainer_lens.agents.investigation.tool_node import (
    ToolSearchRunner,
    make_tool_search_node,
    route_after_tool_search,
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
    query_planner: FollowUpQueryPlanner | None = None,
    tool_search_runner: ToolSearchRunner | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Build retrieval, analysis, and optional LLM query planning."""

    if type(max_search_rounds) is not int or not 1 <= max_search_rounds <= 3:
        raise ValueError("max_search_rounds must be between 1 and 3")

    if query_planner is not None and analyzer is None:
        raise ValueError("query_planner requires an analyzer")

    if query_planner is not None and max_search_rounds == 1:
        raise ValueError("query_planner requires max_search_rounds greater than 1")

    if tool_search_runner is not None:
        if analyzer is None:
            raise ValueError("tool_search_runner requires an analyzer")

        if max_search_rounds == 1:
            raise ValueError(
                "tool_search_runner requires max_search_rounds greater than 1"
            )

        if query_planner is not None:
            raise ValueError(
                "Cannot enable query_planner and tool_search_runner together"
            )

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
            if tool_search_runner is not None:
                builder.add_node(
                    "execute_search_tool",
                    make_tool_search_node(tool_search_runner),
                )

                builder.add_conditional_edges(
                    "execute_search_tool",
                    route_after_tool_search,
                    {
                        "analyze": "analyze_evidence",
                        "stop": "mark_more_evidence_needed",
                    },
                )

                path_map["tool"] = "execute_search_tool"

            elif query_planner is not None:
                builder.add_node(
                    "plan_follow_up_query",
                    make_query_planning_node(query_planner),
                )
                builder.add_node(
                    "apply_planned_query",
                    apply_planned_query,
                )

                builder.add_conditional_edges(
                    "plan_follow_up_query",
                    route_after_planning,
                    {
                        "search": "apply_planned_query",
                        "stop": "mark_more_evidence_needed",
                    },
                )

                builder.add_edge(
                    "apply_planned_query",
                    "search_history",
                )

                path_map["plan"] = "plan_follow_up_query"

            else:
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
            make_analysis_router(
                max_search_rounds,
                use_query_planner=query_planner is not None,
                use_tool_search=tool_search_runner is not None,
            ),
            path_map,
        )

        builder.add_edge("mark_ready_for_report", END)
        builder.add_edge("mark_more_evidence_needed", END)
    else:
        builder.add_edge(last_search_node, END)

    return (
        builder.compile(checkpointer=checkpointer)
        if checkpointer is not None
        else builder.compile()
    )
