"""Claim-scoped progress reporting and allowlisted failure descriptions."""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_progress import append_task_event
from maintainer_lens.schemas.investigation_progress import (
    InvestigationErrorInfo,
    InvestigationPhase,
)


def describe_failure(error: Exception, *, retryable: bool) -> InvestigationErrorInfo:
    """Inspect types only; never persist str(error), prompts or provider bodies."""
    leaves, seen = [], set()

    def collect(exc):
        if id(exc) in seen:
            return
        seen.add(id(exc))
        if isinstance(exc, ExceptionGroup):
            for child in exc.exceptions:
                collect(child)
        else:
            leaves.append(exc)
            if exc.__cause__ is not None:
                collect(exc.__cause__)

    collect(error)
    descriptions = {
        "GitHubRateLimitError": (
            "github_rate_limited",
            "GitHub rate limit reached; retry is deferred.",
        ),
        "InvestigationDeadlineExceeded": (
            "graph_deadline",
            "The investigation execution deadline expired.",
        ),
        "KnowledgePreparationDeadlineExceeded": (
            "preparation_deadline",
            "The knowledge preparation deadline expired.",
        ),
        "KnowledgePreparationBudgetExceeded": (
            "preparation_budget",
            "The selected knowledge preparation budget was exceeded.",
        ),
        "InvestigationSourceIndexNotReadyError": (
            "source_not_ready",
            "The requested Source index is not ready.",
        ),
        "InvalidInvestigationCheckpointError": (
            "invalid_checkpoint",
            "Saved investigation progress is invalid or incompatible.",
        ),
        "InvalidInvestigationResultError": (
            "invalid_result",
            "Analysis, citations or investigation output failed validation.",
        ),
    }
    for leaf in leaves:
        if type(leaf).__name__ == "AnalysisGenerationIncompleteError":
            limited = getattr(leaf, "finish_reason", None) == "length"
            return InvestigationErrorInfo(
                code="analysis_output_limit" if limited else "analysis_incomplete",
                message="Model analysis reached its output allowance."
                if limited
                else "Model generation did not complete a valid analysis.",
                retryable=retryable,
            )
        description = descriptions.get(type(leaf).__name__)
        if description is not None:
            return InvestigationErrorInfo(
                code=description[0],
                message=description[1],
                retryable=retryable,
                not_before=getattr(leaf, "not_before", None),
            )
    http_failure = next(
        (leaf for leaf in leaves if isinstance(leaf, httpx.HTTPStatusError)), None
    )
    if http_failure is not None:
        github = http_failure.request.url.host == "api.github.com"
        status = http_failure.response.status_code
        return InvestigationErrorInfo(
            code=f"{'github' if github else 'upstream'}_http_{status}",
            message=f"{'GitHub' if github else 'Upstream service'} returned HTTP {status}.",
            retryable=retryable,
        )
    if any(isinstance(leaf, (TimeoutError, httpx.TimeoutException)) for leaf in leaves):
        code, message = "upstream_timeout", "An upstream service timed out."
    elif any(isinstance(leaf, (httpx.HTTPError,)) for leaf in leaves):
        code, message = (
            "upstream_unavailable",
            "An upstream service could not complete the request.",
        )
    elif any(isinstance(leaf, ValueError) for leaf in leaves):
        code, message = (
            "invalid_data",
            "Investigation input or upstream data failed validation.",
        )
    else:
        code, message = (
            "processing_failed",
            "Investigation processing failed; inspect server diagnostics.",
        )
    return InvestigationErrorInfo(code=code, message=message, retryable=retryable)


class InvestigationProgressReporter:
    """One reporter per claim; each event owns a new, short Session."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_id: str,
        claim_token: str,
    ):
        self.session_factory, self.task_id, self.claim_token = (
            session_factory,
            task_id,
            claim_token,
        )

    async def phase(self, phase: InvestigationPhase, search_round: int | None = None):
        async with self.session_factory() as session, session.begin():
            await append_task_event(
                session,
                task_id=self.task_id,
                claim_token=self.claim_token,
                kind="progress",
                phase=phase,
                search_round=search_round,
            )

    async def node(self, name: str, state: dict):
        phases = {
            "prepare_query": "preparing_query",
            "search_history": "searching_history",
            "search_source": "searching_source",
            "analyze_evidence": "analyzing",
            "plan_follow_up_query": "planning",
            "apply_planned_query": "planning",
            "prepare_follow_up_query": "planning",
            "execute_search_tool": "tool_search",
            "mark_ready_for_report": "finalizing",
            "mark_more_evidence_needed": "finalizing",
        }
        round_number = state.get("search_round")
        if round_number is not None and name in (
            "execute_search_tool",
            "plan_follow_up_query",
            "apply_planned_query",
            "prepare_follow_up_query",
        ):
            round_number += 1
        await self.phase(phases[name], round_number)
