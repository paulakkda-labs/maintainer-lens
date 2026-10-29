"""Resume trusted Graph progress while preserving the M4 execution boundary."""

import asyncio
from importlib.metadata import version

from langgraph.checkpoint.base import CheckpointTuple

from maintainer_lens.agents.investigation.checkpointing import (
    InvestigationCheckpointSaver,
)
from maintainer_lens.agents.investigation.query_planning import FollowUpQueryPlanner
from maintainer_lens.agents.investigation.state import HistorySearcher, SourceSearcher
from maintainer_lens.agents.investigation.tool_node import ToolSearchRunner
from maintainer_lens.agents.investigation_analysis import InvestigationAnalyzer
from maintainer_lens.db.investigation_checkpoints import (
    InvalidInvestigationCheckpointError,
    InvestigationCheckpointStore,
)
from maintainer_lens.schemas.investigation_checkpoint import (
    GRAPH_EXECUTION_VERSION,
    CheckpointEvidenceContext,
    FrozenInvestigationRun,
    GraphCheckpointConfig,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationResult
from maintainer_lens.services.investigation_execution import (
    InvestigationDeadlineExceeded,
    _prepare_execution,
    _validate_result,
    validate_investigation_evidence,
)


def validate_execution_compatibility(
    execution: GraphCheckpointConfig,
) -> GraphCheckpointConfig:
    """Reject unsupported code/runtime versions before invoking dependencies."""
    execution = GraphCheckpointConfig.model_validate(
        execution.model_dump(), strict=True
    )
    if (
        execution.graph_version != GRAPH_EXECUTION_VERSION
        or execution.langgraph_version != version("langgraph")
        or execution.checkpoint_library_version != version("langgraph-checkpoint")
    ):
        raise InvalidInvestigationCheckpointError(
            "Graph code or runtime version is incompatible with its saved execution"
        )
    return execution


async def load_checkpointed_investigation(
    store: InvestigationCheckpointStore, execution: GraphCheckpointConfig
) -> FrozenInvestigationRun | None:
    execution = validate_execution_compatibility(execution)
    run = await store.load_run()
    if run is not None and run.execution != execution:
        raise InvalidInvestigationCheckpointError(
            "Current Graph configuration differs from its frozen execution"
        )
    return run


def _validate_resume_scope(saved: CheckpointTuple, run: FrozenInvestigationRun) -> None:
    """Check request, evidence and round boundaries before any resumed node runs."""
    values = dict(saved.checkpoint["channel_values"])
    expected = run.request.model_dump(mode="json")
    try:
        # Pending node results are also replayed before the next node executes.
        for _, channel, value in saved.pending_writes or ():
            if channel in expected and value != expected[channel]:
                raise ValueError("Pending write changed request identity")
            if channel in expected or channel in (
                "__start__",
                "history_evidence",
                "source_evidence",
                "analysis_evidence",
                "preliminary_analysis",
                "search_round",
                "no_new_evidence",
            ):
                values[channel] = value
        candidates = []
        if "__start__" in values:
            candidates.append(values["__start__"])
        if any(key in values for key in ("repository", "issue_number", "issue_title")):
            candidates.append(values)
        if not candidates:
            raise ValueError("Checkpoint has no investigation identity")
        for candidate in candidates:
            if not isinstance(candidate, dict) or any(
                candidate.get(key) != value for key, value in expected.items()
            ):
                raise ValueError("Checkpoint request identity mismatch")
        if "search_round" in values and (
            type(values["search_round"]) is not int
            or not 1 <= values["search_round"] <= run.execution.max_search_rounds
        ):
            raise ValueError("Checkpoint search round is outside its budget")
        if "no_new_evidence" in values and type(values["no_new_evidence"]) is not bool:
            raise ValueError("Checkpoint novelty flag is invalid")
        context = CheckpointEvidenceContext.model_validate(
            # Graph stores the UUID as text. After exact identity comparison,
            # restore the backend's typed request for strict Pydantic validation.
            {**values, **run.request.model_dump()},
            strict=True,
        )
        validate_investigation_evidence(context)
    except (TypeError, ValueError, KeyError) as exc:
        raise InvalidInvestigationCheckpointError(
            "Checkpoint cannot safely resume this investigation"
        ) from exc


async def execute_checkpointed_investigation(
    run: FrozenInvestigationRun,
    store: InvestigationCheckpointStore,
    *,
    history_searcher: HistorySearcher,
    analyzer: InvestigationAnalyzer,
    source_searcher: SourceSearcher | None = None,
    query_planner: FollowUpQueryPlanner | None = None,
    tool_search_runner: ToolSearchRunner | None = None,
) -> InvestigationResult:
    """Run one active segment; restored rounds are never reset to fresh input.

    Synchronous checkpoint durability is required before the next super-step.
    The segment deadline includes checkpoint reads/writes, but excludes knowledge
    preparation, frozen-run setup and the final business result transaction.
    """
    run = FrozenInvestigationRun.model_validate_json(run.model_dump_json(), strict=True)
    if run.task_id != store.task_id:
        raise InvalidInvestigationCheckpointError(
            "Frozen execution belongs to another task"
        )
    execution = validate_execution_compatibility(run.execution)
    strategy = (
        "tool"
        if tool_search_runner is not None
        else "query_planner"
        if query_planner is not None
        else "deterministic"
    )
    if execution.search_strategy != strategy or execution.source_enabled != (
        source_searcher is not None
    ):
        raise ValueError(
            "Graph dependencies do not match the frozen execution strategy"
        )
    saver = InvestigationCheckpointSaver(store)
    request, graph, initial_state = _prepare_execution(
        run.request,
        history_searcher=history_searcher,
        analyzer=analyzer,
        timeout_seconds=execution.timeout_seconds,
        source_searcher=source_searcher,
        max_search_rounds=execution.max_search_rounds,
        query_planner=query_planner,
        tool_search_runner=tool_search_runner,
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": run.task_id}}
    timeout_scope = asyncio.timeout(execution.timeout_seconds)
    try:
        async with timeout_scope:
            saved = await saver.aget_tuple(config)
            if saved is not None:
                _validate_resume_scope(saved, run)
            operation = asyncio.create_task(
                graph.ainvoke(
                    None if saved is not None else initial_state,
                    config,
                    durability="sync",
                )
            )
            try:
                # Shield only until we can cancel Saver background I/O as well.
                # LangGraph otherwise waits for those callbacks on context exit.
                state = await asyncio.shield(operation)
            except BaseException:
                saver.cancel_pending(exclude=operation)
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                raise
    except TimeoutError as exc:
        if not timeout_scope.expired():
            raise
        raise InvestigationDeadlineExceeded("Investigation deadline exceeded") from exc
    if timeout_scope.expired():
        raise InvestigationDeadlineExceeded("Investigation deadline exceeded")
    if "__interrupt__" in state:
        raise InvalidInvestigationCheckpointError(
            "Human review is not enabled for this execution version"
        )
    return _validate_result(state, request, execution.max_search_rounds)
