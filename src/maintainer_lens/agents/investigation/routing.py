"""Query preparation and bounded investigation routing decisions."""

from uuid import UUID

from maintainer_lens.agents.investigation.state import (
    AnalysisRoute,
    InvestigationState,
)


def make_prepare_query_node(*, require_source_index: bool):
    """Create the initial issue-query preparation node."""

    def prepare_query(state: InvestigationState) -> dict:
        repository = state["repository"].strip()
        issue_number = state["issue_number"]
        title = state["issue_title"].strip()
        body = (state["issue_body"] or "").strip()

        if not repository:
            raise ValueError("Repository must not be blank")

        if type(issue_number) is not int or issue_number <= 0:
            raise ValueError("Issue number must be a positive integer")

        if not title:
            raise ValueError("Issue title must not be blank")

        response_language = state.get("response_language", "en")

        if response_language not in ("zh", "en"):
            raise ValueError("response_language must be 'zh' or 'en'")

        query = f"{title}\n{body[:800]}".strip()

        updates = {
            "repository": repository,
            "history_query": query,
            "response_language": response_language,
            "search_round": 1,
            "searched_queries": [query],
            "new_history_evidence": 0,
            "new_source_evidence": 0,
            "no_new_evidence": False,
        }

        if require_source_index:
            raw_index_id = state.get("source_index_id")

            if not isinstance(raw_index_id, str) or not raw_index_id.strip():
                raise ValueError(
                    "source_index_id is required when source search is enabled"
                )

            try:
                source_index_id = UUID(raw_index_id)
            except ValueError as exc:
                raise ValueError("Invalid source_index_id UUID") from exc

            updates["source_index_id"] = str(source_index_id)

        return updates

    return prepare_query


def select_follow_up_query(state: InvestigationState) -> str | None:
    """Select an unused query from unresolved analysis items."""

    analysis = state.get("preliminary_analysis")

    if analysis is None:
        return None

    previous_queries = {
        " ".join(query.split()).casefold()
        for query in state.get("searched_queries", [])
    }

    candidates = [
        *analysis["unanswered_questions"],
        *analysis["hypotheses"],
    ]

    for text in candidates:
        query = " ".join(text.split()[:16])[:256].strip()

        if not query:
            continue

        if query.casefold() in previous_queries:
            continue

        return query

    return None


def prepare_follow_up_query(state: InvestigationState) -> dict:
    """Advance the investigation by one search round."""

    query = select_follow_up_query(state)

    if query is None:
        raise RuntimeError("No unused follow-up query is available")

    return {
        "history_query": query,
        "search_round": state["search_round"] + 1,
        "searched_queries": [
            *state["searched_queries"],
            query,
        ],
        "new_history_evidence": 0,
        "new_source_evidence": 0,
        "no_new_evidence": False,
    }


def assess_next_step(
    state: InvestigationState,
) -> tuple[str, str]:
    """Assess whether the investigation has unresolved evidence gaps."""

    analysis = state.get("preliminary_analysis")

    if analysis is None:
        raise ValueError("Preliminary analysis is missing")

    if state.get("no_new_evidence", False):
        return "follow_up", "The follow-up search found no new evidence."

    if not state.get("history_evidence") and not state.get("source_evidence"):
        return (
            "follow_up",
            "No source or historical evidence was retrieved.",
        )

    if not analysis["findings"]:
        return (
            "follow_up",
            "The preliminary analysis contains no supported findings.",
        )

    if analysis["unanswered_questions"]:
        return (
            "follow_up",
            "The preliminary analysis contains unanswered questions.",
        )

    if analysis["hypotheses"]:
        return (
            "follow_up",
            "The preliminary analysis contains unverified hypotheses.",
        )

    return (
        "ready",
        "The preliminary analysis has cited findings and no declared gaps.",
    )


def make_analysis_router(max_search_rounds: int):
    """Create the routing function with a fixed search-round budget."""

    def route_after_analysis(state: InvestigationState) -> AnalysisRoute:
        route, _ = assess_next_step(state)

        if route == "ready":
            return "ready"

        if state["search_round"] >= max_search_rounds:
            return "follow_up"

        if not state.get("history_evidence") and not state.get("source_evidence"):
            return "follow_up"

        if state.get("no_new_evidence", False):
            return "follow_up"

        if select_follow_up_query(state) is None:
            return "follow_up"

        return "retry"

    return route_after_analysis


def mark_ready_for_report(state: InvestigationState) -> dict:
    """Mark an investigation ready for preliminary report drafting."""

    _, reason = assess_next_step(state)

    return {
        "next_step": "draft_preliminary_report",
        "routing_reason": reason,
    }


def mark_more_evidence_needed(state: InvestigationState) -> dict:
    """Mark an investigation as requiring further evidence."""

    _, reason = assess_next_step(state)

    return {
        "next_step": "needs_more_evidence",
        "routing_reason": reason,
    }
