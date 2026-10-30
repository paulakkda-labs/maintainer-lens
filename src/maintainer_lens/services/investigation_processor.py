"""Compose snapshot acquisition, Graph execution and durable result storage."""

from collections.abc import Awaitable, Callable
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
from maintainer_lens.db.investigation_checkpoints import InvestigationCheckpointStore
from maintainer_lens.db.investigation_preparation import (
    bind_prepared_source_index,
    get_claimed_knowledge_preparation,
)
from maintainer_lens.db.investigation_results import upsert_investigation_result
from maintainer_lens.db.investigation_tasks import get_claimed_investigation_identity
from maintainer_lens.investigation_runner import (
    LeaseLostError,
    PermanentInvestigationError,
)
from maintainer_lens.schemas.investigation_checkpoint import GraphCheckpointConfig
from maintainer_lens.schemas.investigation_contracts import (
    InvestigationIdentity,
    InvestigationInput,
)
from maintainer_lens.schemas.investigation_review import (
    HUMAN_ACCEPTANCE_REASON,
    ReviewContext,
    ReviewDecision,
)
from maintainer_lens.services.investigation_checkpoint_execution import (
    execute_checkpointed_investigation,
    load_checkpointed_investigation,
    validate_execution_compatibility,
)
from maintainer_lens.services.investigation_execution import (
    _validate_result,
    execute_graph_investigation,
)
from maintainer_lens.services.investigation_preparation import KnowledgePreparer
from maintainer_lens.services.investigation_progress import (
    InvestigationProgressReporter,
)
from maintainer_lens.services.investigation_review import apply_approval
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
    source_index_validator: Callable[[str, UUID], Awaitable[None]] | None = None,
    max_search_rounds: int = 1,
    query_planner: FollowUpQueryPlanner | None = None,
    tool_search_runner: ToolSearchRunner | None = None,
    auto_prepare_knowledge: bool = False,
    knowledge_preparer: KnowledgePreparer | None = None,
    checkpoint_store: InvestigationCheckpointStore | None = None,
    checkpoint_config: GraphCheckpointConfig | None = None,
    progress: InvestigationProgressReporter | None = None,
    review_approval: tuple[ReviewContext, ReviewDecision] | None = None,
) -> None:
    """Process an existing claim, returning only after its result commits.

    The caller owns clients and the claim/lease lifecycle. Automatic mode uses
    its stored budget and binds the prepared identity before Graph execution.
    Checkpoint-enabled retries reuse a frozen input before snapshot acquisition.
    Graph options are validated by the application entry after snapshot commit;
    its deadline does not cover snapshot acquisition or database commits.
    """
    requested_identity = InvestigationIdentity.model_validate(
        {
            "repository": repository,
            "issue_number": issue_number,
            "source_index_id": source_index_id,
        },
        strict=True,
    )
    if type(auto_prepare_knowledge) is not bool:
        raise ValueError("Automatic preparation flag must be a boolean")
    if auto_prepare_knowledge and (
        source_index_id is not None
        or source_searcher is None
        or source_index_validator is None
        or knowledge_preparer is None
    ):
        raise ValueError(
            "Automatic preparation requires Source dependencies and no requested index"
        )

    if (checkpoint_store is None) != (checkpoint_config is None):
        raise ValueError("Checkpoint store and configuration must be supplied together")
    if checkpoint_store is not None:
        if (checkpoint_store.task_id, checkpoint_store.claim_token) != (
            task_id,
            claim_token,
        ):
            raise ValueError("Checkpoint store belongs to another claim")
        checkpoint_config = validate_execution_compatibility(checkpoint_config)
        strategy = (
            "tool"
            if tool_search_runner is not None
            else "query_planner"
            if query_planner is not None
            else "deterministic"
        )
        if (
            checkpoint_config.response_language != response_language
            or checkpoint_config.max_search_rounds != max_search_rounds
            or checkpoint_config.timeout_seconds != timeout_seconds
            or checkpoint_config.source_enabled != (source_searcher is not None)
            or checkpoint_config.search_strategy != strategy
        ):
            raise ValueError("Processor options do not match checkpoint configuration")

    if review_approval is not None:
        review_context, review_decision = review_approval
        if checkpoint_config != review_context.base_execution:
            raise ValueError(
                "Approved review differs from the current execution identity"
            )

    preparation = None
    async with session_factory() as session:
        async with session.begin():
            mode_options = (
                {"auto_prepare_knowledge": True} if auto_prepare_knowledge else {}
            )
            if response_language != "zh":
                mode_options["response_language"] = response_language
            identity = await get_claimed_investigation_identity(
                session, task_id=task_id, claim_token=claim_token, **mode_options
            )
            if identity is None:
                raise LeaseLostError(
                    "Investigation claim is missing or no longer active"
                )
            if identity != (
                requested_identity.repository,
                requested_identity.issue_number,
                requested_identity.source_index_id,
            ):
                raise PermanentInvestigationError(
                    "Investigation request does not match its task identity"
                )
            if auto_prepare_knowledge:
                preparation = await get_claimed_knowledge_preparation(
                    session, task_id=task_id, claim_token=claim_token
                )
                if preparation is None:
                    raise LeaseLostError(
                        "Automatic preparation claim is no longer active"
                    )
                if (preparation.repository, preparation.issue_number) != identity[:2]:
                    raise PermanentInvestigationError(
                        "Preparation identity changed during preflight"
                    )

    frozen_run = (
        await load_checkpointed_investigation(checkpoint_store, checkpoint_config)
        if checkpoint_store is not None
        else None
    )
    task_repository, task_issue_number, task_source_index_id = identity
    if preparation is not None:
        task_source_index_id = preparation.prepared_source_index_id
    if task_source_index_id is not None:
        if source_searcher is None:
            raise ValueError("Requested Source index requires a source_searcher")
        if source_index_validator is not None:
            await source_index_validator(task_repository, task_source_index_id)
    elif source_searcher is not None and not auto_prepare_knowledge:
        raise ValueError("Source search requires a task source_index_id")
    if frozen_run is not None:
        request = frozen_run.request
    else:
        if progress is not None:
            await progress.phase("snapshot")
        snapshot = await investigate_issue(
            http_client=http_client,
            session_factory=session_factory,
            task_id=task_id,
            claim_token=claim_token,
            repository=task_repository,
            issue_number=task_issue_number,
        )
        if preparation is not None and task_source_index_id is None:
            task_source_index_id = await knowledge_preparer(
                task_repository,
                apply_approval(*review_approval)[0]
                if review_approval is not None
                else preparation.budget,
            )
            await source_index_validator(task_repository, task_source_index_id)
            async with session_factory() as session, session.begin():
                bound = await bind_prepared_source_index(
                    session,
                    task_id=task_id,
                    claim_token=claim_token,
                    repository=task_repository,
                    issue_number=task_issue_number,
                    source_index_id=task_source_index_id,
                )
                if not bound:
                    raise LeaseLostError(
                        "Investigation claim was lost before index binding"
                    )
        request = InvestigationInput(
            repository=task_repository,
            issue_number=task_issue_number,
            issue_title=snapshot.title,
            issue_body=snapshot.body,
            response_language=response_language,
            source_index_id=task_source_index_id,
        )
        if checkpoint_store is not None:
            frozen_run = await checkpoint_store.create_run(request, checkpoint_config)

    if review_approval is not None and review_decision.action == "accept_partial":
        if (
            frozen_run is None
            or frozen_run.fingerprint != review_context.run_fingerprint
        ):
            raise ValueError("Accepted analysis has a different frozen execution")
        result = _validate_result(
            {
                **review_context.partial_result.model_dump(mode="json"),
                "routing_reason": HUMAN_ACCEPTANCE_REASON,
                "no_new_evidence": review_context.partial_no_new_evidence,
            },
            frozen_run.request,
            frozen_run.execution.max_search_rounds,
        )
    elif checkpoint_store is not None:
        result = await execute_checkpointed_investigation(
            frozen_run,
            checkpoint_store,
            history_searcher=history_searcher,
            analyzer=analyzer,
            source_searcher=source_searcher,
            query_planner=query_planner,
            tool_search_runner=tool_search_runner,
            **({"node_observer": progress.node} if progress is not None else {}),
        )
    else:
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

    if progress is not None:
        await progress.phase("finalizing", result.search_round)
    async with session_factory() as session:
        async with session.begin():
            saved = await upsert_investigation_result(
                session, task_id=task_id, claim_token=claim_token, result=result
            )
            if not saved:
                raise LeaseLostError(
                    "Investigation claim was lost before result storage"
                )
