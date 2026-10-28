"""Compose snapshot acquisition, Graph execution and durable result storage."""

from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.agents.investigation.query_planning import FollowUpQueryPlanner
from maintainer_lens.agents.investigation.state import HistorySearcher, SourceSearcher
from maintainer_lens.agents.investigation.tool_node import ToolSearchRunner
from maintainer_lens.agents.investigation_analysis import (
    InvestigationAnalyzer,
    ResponseLanguage,
)
from maintainer_lens.db.investigation_results import upsert_investigation_result
from maintainer_lens.db.investigation_tasks import get_claimed_investigation_identity
from maintainer_lens.investigation_runner import (
    LeaseLostError,
    PermanentInvestigationError,
)
from maintainer_lens.schemas.investigation_contracts import (
    CreateInvestigationRequest,
    InvestigationInput,
)
from maintainer_lens.services.investigation_execution import execute_graph_investigation
from maintainer_lens.services.issue_investigator import investigate_issue


async def process_graph_investigation(
    *,
    http_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    claim_token: str,
    repository: str,
    issue_number: int,
    response_language: ResponseLanguage,
    history_searcher: HistorySearcher,
    analyzer: InvestigationAnalyzer,
    timeout_seconds: float,
    source_index_id: UUID | None = None,
    source_searcher: SourceSearcher | None = None,
    max_search_rounds: int = 1,
    query_planner: FollowUpQueryPlanner | None = None,
    tool_search_runner: ToolSearchRunner | None = None,
) -> None:
    """Process an existing claim, returning only after its result commits.

    The caller owns clients, knowledge preparation and the claim/lease lifecycle.
    Graph options are validated by the application entry after snapshot commit;
    its deadline does not cover snapshot acquisition or database commits.
    """
    requested_identity = CreateInvestigationRequest.model_validate(
        {"repository": repository, "issue_number": issue_number}, strict=True
    )

    async with session_factory() as session:
        async with session.begin():
            identity = await get_claimed_investigation_identity(
                session, task_id=task_id, claim_token=claim_token
            )
            if identity is None:
                raise LeaseLostError(
                    "Investigation claim is missing or no longer active"
                )
            if identity != (
                requested_identity.repository,
                requested_identity.issue_number,
            ):
                raise PermanentInvestigationError(
                    "Investigation request does not match its task identity"
                )

    task_repository, task_issue_number = identity
    snapshot = await investigate_issue(
        http_client=http_client,
        session_factory=session_factory,
        task_id=task_id,
        claim_token=claim_token,
        repository=task_repository,
        issue_number=task_issue_number,
    )
    request = InvestigationInput(
        repository=task_repository,
        issue_number=task_issue_number,
        issue_title=snapshot.title,
        issue_body=snapshot.body,
        response_language=response_language,
        source_index_id=source_index_id,
    )
    result = await execute_graph_investigation(
        request,
        history_searcher=history_searcher,
        analyzer=analyzer,
        timeout_seconds=timeout_seconds,
        source_searcher=source_searcher,
        max_search_rounds=max_search_rounds,
        query_planner=query_planner,
        tool_search_runner=tool_search_runner,
    )

    async with session_factory() as session:
        async with session.begin():
            saved = await upsert_investigation_result(
                session, task_id=task_id, claim_token=claim_token, result=result
            )
            if not saved:
                raise LeaseLostError(
                    "Investigation claim was lost before result storage"
                )
