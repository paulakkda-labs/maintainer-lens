"""Real compiled Graph and Saver recovery with isolated in-memory storage I/O.

This suite does not verify PostgreSQL fencing. Its storage fake is task-scoped;
real transaction/lease checks are exercised by the separate opt-in PG suite.
No settings, .env, broker or paid provider calls are used here.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.types import ERROR
from langgraph.graph import END, START, StateGraph

import maintainer_lens.services.investigation_checkpoint_execution as execution
from maintainer_lens.agents.investigation.checkpointing import (
    InvestigationCheckpointSaver,
)
from maintainer_lens.agents.investigation.evidence import to_history_evidence
from maintainer_lens.agents.investigation_analysis import InvestigationAnalysis
from maintainer_lens.db.investigation_checkpoints import (
    InvalidInvestigationCheckpointError,
    StoredCheckpointWrite,
)
from maintainer_lens.investigation_runner import (
    LeaseLostError,
    RetryableInvestigationError,
)
from maintainer_lens.schemas.investigation_checkpoint import (
    FrozenInvestigationRun,
    GraphCheckpointConfig,
    endpoint_fingerprint,
    execution_fingerprint,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationInput
from maintainer_lens.services.investigation_execution import (
    InvestigationDeadlineExceeded,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)
from maintainer_lens.services.repository_hybrid_search import HybridSearchResult

pytestmark = pytest.mark.anyio
REPOSITORY = "checkpoint-tests/repository"
TASK_ID = "checkpoint-task"
INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _config(**changes):
    return GraphCheckpointConfig.model_validate(
        {
            "response_language": "en",
            "max_search_rounds": 3,
            "timeout_seconds": 5.0,
            "search_strategy": "deterministic",
            "source_enabled": False,
            "history_top_k": 5,
            "source_top_k": 5,
            "embedding_provider": "test",
            "embedding_model": "embedding",
            "embedding_dimensions": 3,
            "embedding_endpoint_fingerprint": endpoint_fingerprint(
                "https://embedding.example/v1"
            ),
            "llm_model": "analysis",
            "llm_endpoint_fingerprint": endpoint_fingerprint("https://llm.example/v1"),
            "llm_max_output_tokens": 4096,
            "thinking_enabled": False,
            **changes,
        }
    )


def _run(**changes):
    config = _config(**changes)
    request = InvestigationInput(
        repository=REPOSITORY,
        issue_number=9000,
        issue_title="Cancellation cleanup",
        issue_body=None,
        response_language="en",
        source_index_id=INDEX_ID if config.source_enabled else None,
    )
    return FrozenInvestigationRun(
        task_id=TASK_ID,
        request=request,
        execution=config,
        fingerprint=execution_fingerprint(request, config),
    )


class _MemoryStore:
    """Replace SQL, not Saver serialization or compiled Graph execution."""

    def __init__(self, run=None):
        self.task_id, self.claim_token = TASK_ID, "claim"
        self.run = run or _run()
        self.frames, self.writes = {}, {}
        self.active = True
        self.calls = []

    def _owned(self):
        if not self.active:
            raise LeaseLostError("Lost fake claim")

    async def load_run(self):
        self._owned()
        return self.run

    async def read_checkpoint(self, namespace, checkpoint_id=None):
        self._owned()
        self.calls.append("read")
        ids = [key[1] for key in self.frames if key[0] == namespace]
        checkpoint_id = (
            checkpoint_id if checkpoint_id is not None else max(ids, default=None)
        )
        row = self.frames.get((namespace, checkpoint_id))
        if row is None:
            return None
        writes = tuple(
            value
            for (ns, cid, _, _), value in self.writes.items()
            if (ns, cid) == (namespace, checkpoint_id)
        )
        return replace(row, metadata=deepcopy(row.metadata), writes=writes)

    async def write_checkpoint(self, row):
        self._owned()
        self.calls.append("checkpoint")
        self.frames[(row.namespace, row.checkpoint_id)] = row

    async def write_nodes(self, namespace, checkpoint_id, rows):
        self._owned()
        self.calls.append("writes")
        for row in rows:
            key = namespace, checkpoint_id, row.node_task_id, row.write_index
            if row.write_index < 0 or key not in self.writes:
                self.writes[key] = row

    async def list_checkpoints(
        self, *, namespace, checkpoint_id, before_id, metadata_filter, limit
    ):
        result = []
        for ns, cid in sorted(self.frames, key=lambda key: key[1], reverse=True):
            row = await self.read_checkpoint(ns, cid)
            if (
                namespace is not None
                and ns != namespace
                or checkpoint_id is not None
                and cid != checkpoint_id
            ):
                continue
            if before_id is not None and cid >= before_id:
                continue
            if metadata_filter and any(
                row.metadata.get(key) != value for key, value in metadata_filter.items()
            ):
                continue
            result.append(row)
        return result if limit is None else result[:limit]

    async def delete_checkpoints(self):
        self._owned()
        self.frames.clear()
        self.writes.clear()


def _hit(number=1):
    return RepositoryHistorySemanticHit(
        repository=REPOSITORY,
        number=number,
        source_type="item",
        comment_id=None,
        title=f"Discussion {number}",
        content="Cancellation cleanup requires finally.",
        source_url=f"https://github.com/{REPOSITORY}/issues/{number}",
        source_updated_at=datetime(2026, 10, 29, tzinfo=UTC),
        distance=0.1,
    )


def _analysis(request, *, needs_more=False):
    return InvestigationAnalysis(
        summary="Relevant evidence.",
        findings=[
            {
                "statement": "Cleanup is discussed.",
                "evidence_ids": [request["evidence"][0]["evidence_id"]],
            }
        ],
        hypotheses=[],
        unanswered_questions=["What remains unverified?"] if needs_more else [],
    )


async def _execute(store, *, analyzer=None, history=None, tool=None):
    return await execution.execute_checkpointed_investigation(
        store.run,
        store,
        history_searcher=history
        if history is not None
        else AsyncMock(return_value=(_hit(),)),
        analyzer=analyzer if analyzer is not None else AsyncMock(side_effect=_analysis),
        tool_search_runner=tool,
    )


def _configuration(checkpoint_id=None, *, namespace="", thread_id=TASK_ID):
    values = {"thread_id": thread_id, "checkpoint_ns": namespace}
    if checkpoint_id is not None:
        values["checkpoint_id"] = checkpoint_id
    return {"configurable": values}


@pytest.mark.parametrize("format_version", [2, 4])
async def test_saver_preserves_full_snapshot_parent_and_typed_values(format_version):
    store, frame = _MemoryStore(), empty_checkpoint()
    frame["v"] = format_version
    frame["channel_values"] = {"value": {"list": [1, None, False], "bytes": b"x"}}
    first = await InvestigationCheckpointSaver(store).aput(
        _configuration(), frame, {"step": 0}, {}
    )
    second = empty_checkpoint()
    second["v"] = format_version
    second["channel_values"] = {"value": "next"}
    saved = await InvestigationCheckpointSaver(store).aput(
        first, second, {"step": 1}, {}
    )
    restored = await InvestigationCheckpointSaver(store).aget_tuple(saved)
    assert restored.checkpoint == second and restored.parent_config == first
    assert (
        await InvestigationCheckpointSaver(store).aget_tuple(first)
    ).checkpoint == frame


@pytest.mark.parametrize("format_version", [None, True, 0, -1, 1, 3, 5, 4.0, "4"])
async def test_saver_rejects_unknown_or_non_integer_format_before_storage(
    format_version,
):
    store, frame = _MemoryStore(), empty_checkpoint()
    frame["v"] = format_version
    with pytest.raises(InvalidInvestigationCheckpointError, match="structure"):
        await InvestigationCheckpointSaver(store).aput(_configuration(), frame, {}, {})
    assert not store.calls and not store.frames


@pytest.mark.parametrize("format_version", [True, 5, "4"])
async def test_saver_rejects_corrupt_stored_format_without_rewriting_it(format_version):
    store, frame = _MemoryStore(), empty_checkpoint()
    saver = InvestigationCheckpointSaver(store)
    config = await saver.aput(_configuration(), frame, {}, {})
    frame["v"] = format_version
    payload_type, payload = saver.serde.dumps_typed(frame)
    key = "", frame["id"]
    corrupt = replace(store.frames[key], payload_type=payload_type, payload=payload)
    store.frames[key] = corrupt
    with pytest.raises(InvalidInvestigationCheckpointError, match="structure"):
        await saver.aget_tuple(config)
    assert store.frames[key] == corrupt


async def test_pending_writes_can_arrive_before_snapshot_and_keep_sdk_dedup_order():
    store, frame = _MemoryStore(), empty_checkpoint()
    saver = InvestigationCheckpointSaver(store)
    config = _configuration(frame["id"])
    await saver.aput_writes(config, [("value", "first")], "node-z", "path-z")
    await saver.aput_writes(config, [("value", "ignored-repeat")], "node-z", "path-z")
    await saver.aput_writes(config, [(ERROR, "old")], "node-z", "path-z")
    await saver.aput_writes(config, [(ERROR, "new")], "node-z", "path-z")
    await saver.aput_writes(config, [("value", "earlier-path")], "node-a", "path-a")
    await saver.aput(_configuration(), frame, {"step": 0}, {})
    result = await saver.aget_tuple(config)
    assert result.pending_writes == [
        ("node-a", "value", "earlier-path"),
        ("node-z", ERROR, "new"),
        ("node-z", "value", "first"),
    ]


async def test_saver_history_filter_before_limit_and_scoped_delete():
    store, saver = _MemoryStore(), None
    saver = InvestigationCheckpointSaver(store)
    configs = []
    for step in range(3):
        frame = empty_checkpoint()
        configs.append(
            await saver.aput(
                configs[-1] if configs else _configuration(),
                frame,
                {"step": step, "kind": "even" if step % 2 == 0 else "odd"},
                {},
            )
        )
    rows = [row async for row in saver.alist(_configuration(), filter={"kind": "even"})]
    assert [row.metadata["step"] for row in rows] == [2, 0]
    rows = [row async for row in saver.alist(None, before=configs[-1], limit=1)]
    assert rows[0].metadata["step"] == 1
    with pytest.raises(InvalidInvestigationCheckpointError):
        await saver.adelete_thread("foreign")
    assert len(store.frames) == 3
    await saver.adelete_thread(TASK_ID)
    assert not store.frames and not store.writes


@pytest.mark.parametrize(
    "config", [_configuration(thread_id="foreign"), {"configurable": {}}, {}, None]
)
async def test_foreign_or_invalid_checkpoint_scope_fails_before_storage(config):
    store = _MemoryStore()
    with pytest.raises(InvalidInvestigationCheckpointError):
        await InvestigationCheckpointSaver(store).aget_tuple(config)
    assert not store.calls


async def test_saver_rejects_pickle_before_deserialization():
    store, frame = _MemoryStore(), empty_checkpoint()
    config = await InvestigationCheckpointSaver(store).aput(
        _configuration(), frame, {}, {}
    )
    key = "", frame["id"]
    store.frames[key] = replace(
        store.frames[key], payload_type="pickle", payload=b"not executed"
    )
    with pytest.raises(InvalidInvestigationCheckpointError, match="serialization"):
        await InvestigationCheckpointSaver(store).aget_tuple(config)


async def test_completed_checkpoint_returns_validated_result_without_recomputing():
    store = _MemoryStore()
    history, analyzer = (
        AsyncMock(return_value=(_hit(),)),
        AsyncMock(side_effect=_analysis),
    )
    first = await _execute(store, history=history, analyzer=analyzer)
    second = await _execute(store, history=history, analyzer=analyzer)
    assert first == second and first.next_step == "draft_preliminary_report"
    assert history.await_count == analyzer.await_count == 1
    assert store.frames and store.writes
    # The real compiled Graph emits v4 even though public empty_checkpoint()
    # emits v2. Do not derive this expected version from the production guard.
    saver = InvestigationCheckpointSaver(store)
    assert {
        saver.serde.loads_typed((row.payload_type, row.payload))["v"]
        for row in store.frames.values()
    } == {4}


async def test_failed_analysis_resumes_after_saved_retrieval_with_new_saver_instance():
    store, failure = (
        _MemoryStore(),
        RetryableInvestigationError("Transient model failure"),
    )
    history = AsyncMock(return_value=(_hit(),))
    analyzer = AsyncMock(side_effect=failure)
    with pytest.raises(RetryableInvestigationError) as caught:
        await _execute(store, history=history, analyzer=analyzer)
    assert caught.value is failure
    analyzer.side_effect = _analysis
    result = await _execute(store, history=history, analyzer=analyzer)
    assert history.await_count == 1 and analyzer.await_count == 2
    assert result.history_evidence and result.analysis_evidence


async def test_third_round_resume_preserves_budget_and_completed_analysis_context():
    store = _MemoryStore(_run(search_strategy="tool", tool_timeout_seconds=1.0))
    history = AsyncMock(return_value=(_hit(),))
    analyzer = AsyncMock(
        side_effect=lambda request: _analysis(request, needs_more=True)
    )
    failed = False
    rounds = []

    async def tool(state):
        nonlocal failed
        rounds.append(state["search_round"])
        if state["search_round"] == 2 and not failed:
            failed = True
            raise RetryableInvestigationError("Third-round search failed")
        number = state["search_round"] + 1
        return {
            "tool_name": "search_history",
            "tool_call_id": f"call-{number}",
            "query": f"cleanup evidence {number}",
            "evidence": [],
            "history_evidence": [to_history_evidence(_hit(number), REPOSITORY)],
            "source_evidence": [],
            "skipped_duplicate": False,
            "model_response": "Evidence found.",
        }

    with pytest.raises(RetryableInvestigationError):
        await _execute(store, history=history, analyzer=analyzer, tool=tool)
    result = await _execute(store, history=history, analyzer=analyzer, tool=tool)
    assert rounds == [1, 2, 2] and result.search_round == 3
    assert result.next_step == "needs_more_evidence"
    assert history.await_count == 1 and analyzer.await_count == 3
    assert len(result.history_evidence) == 3


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("Upstream deadline"),
        ValueError("Dependency validation failure"),
        LeaseLostError("Dependency lease lost"),
        ExceptionGroup("original", [ValueError("one"), RuntimeError("two")]),
    ],
)
async def test_resumed_dependency_errors_keep_identity_and_structure(failure):
    store = _MemoryStore()
    with pytest.raises(type(failure)) as caught:
        await _execute(store, analyzer=AsyncMock(side_effect=failure))
    assert caught.value is failure


@pytest.mark.parametrize("own_deadline", [False, True])
async def test_active_segment_cancellation_cleans_dependency_and_keeps_recovery_progress(
    monkeypatch, own_deadline
):
    store, entered, cleaned = _MemoryStore(), asyncio.Event(), asyncio.Event()
    scopes = []
    real_timeout = asyncio.timeout

    def timeout(seconds):
        scope = real_timeout(seconds)
        scopes.append(scope)
        return scope

    monkeypatch.setattr(
        execution,
        "asyncio",
        SimpleNamespace(
            timeout=timeout,
            create_task=asyncio.create_task,
            shield=asyncio.shield,
            gather=asyncio.gather,
        ),
    )

    async def block(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    history = AsyncMock(return_value=(_hit(),))
    operation = asyncio.create_task(_execute(store, history=history, analyzer=block))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if own_deadline:
            scopes[0].reschedule(asyncio.get_running_loop().time())
        else:
            operation.cancel("external")
        with pytest.raises(
            InvestigationDeadlineExceeded if own_deadline else asyncio.CancelledError
        ):
            await operation
        assert cleaned.is_set()
        result = await _execute(store, history=history)
        assert (
            result.next_step == "draft_preliminary_report" and history.await_count == 1
        )
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_swallowed_own_deadline_cannot_return_success(monkeypatch):
    store, entered, scopes = _MemoryStore(), asyncio.Event(), []
    real_timeout = asyncio.timeout

    def timeout(seconds):
        scope = real_timeout(seconds)
        scopes.append(scope)
        return scope

    monkeypatch.setattr(
        execution,
        "asyncio",
        SimpleNamespace(
            timeout=timeout,
            create_task=asyncio.create_task,
            shield=asyncio.shield,
            gather=asyncio.gather,
        ),
    )

    async def swallow(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return _analysis(request)

    operation = asyncio.create_task(_execute(store, analyzer=swallow))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        scopes[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(InvestigationDeadlineExceeded):
            await operation
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize(
    "change",
    [
        {"llm_model": "other"},
        {"max_search_rounds": 2},
        {"graph_version": "future-version"},
    ],
)
async def test_incompatible_frozen_config_is_not_silently_replaced(change):
    store = _MemoryStore()
    with pytest.raises(InvalidInvestigationCheckpointError):
        await execution.load_checkpointed_investigation(store, _config(**change))
    assert not store.frames


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "foreign/repository"),
        ("source_index_id", "12345678-1234-4234-8234-123456789abc"),
        ("search_round", 9),
    ],
)
async def test_corrupt_checkpoint_boundaries_fail_before_resumed_dependencies(
    field, value
):
    store = _MemoryStore()
    with pytest.raises(RetryableInvestigationError):
        await _execute(
            store, analyzer=AsyncMock(side_effect=RetryableInvestigationError("pause"))
        )
    key = max(store.frames, key=lambda item: item[1])
    saver = InvestigationCheckpointSaver(store)
    row = store.frames[key]
    checkpoint = saver.serde.loads_typed((row.payload_type, row.payload))
    checkpoint["channel_values"][field] = value
    payload_type, payload = saver.serde.dumps_typed(checkpoint)
    store.frames[key] = replace(row, payload_type=payload_type, payload=payload)
    history, analyzer = AsyncMock(), AsyncMock()
    with pytest.raises(InvalidInvestigationCheckpointError):
        await _execute(store, history=history, analyzer=analyzer)
    history.assert_not_awaited()
    analyzer.assert_not_awaited()


async def test_pending_identity_write_is_checked_before_replay():
    store = _MemoryStore()
    with pytest.raises(RetryableInvestigationError):
        await _execute(
            store, analyzer=AsyncMock(side_effect=RetryableInvestigationError("pause"))
        )
    key = max(store.frames, key=lambda item: item[1])
    saver = InvestigationCheckpointSaver(store)
    payload_type, payload = saver.serde.dumps_typed("foreign/repository")
    store.writes[(*key, "malicious", 0)] = StoredCheckpointWrite(
        "malicious", 0, "repository", "", payload_type, payload
    )
    with pytest.raises(InvalidInvestigationCheckpointError):
        await _execute(store)


async def test_saver_does_not_hide_lost_ownership():
    store = _MemoryStore()
    store.active = False
    with pytest.raises(LeaseLostError):
        await InvestigationCheckpointSaver(store).aput(
            _configuration(), empty_checkpoint(), {}, {}
        )
    assert not store.frames


async def test_parallel_node_pending_results_are_reused_after_sibling_failure():
    """Exercise SDK pending-write semantics beyond a single linear node."""
    from typing_extensions import TypedDict

    class State(TypedDict):
        left: int
        right: int

    store = _MemoryStore()
    saver = InvestigationCheckpointSaver(store)
    left, right = (
        AsyncMock(return_value={"left": 1}),
        AsyncMock(side_effect=RuntimeError("retry sibling")),
    )

    async def left_node(state):
        return await left(state)

    async def right_node(state):
        return await right(state)

    builder = StateGraph(State)
    builder.add_node("left_node", left_node)
    builder.add_node("right_node", right_node)
    builder.add_edge(START, "left_node")
    builder.add_edge(START, "right_node")
    builder.add_edge("left_node", END)
    builder.add_edge("right_node", END)
    config = _configuration()
    with pytest.raises(RuntimeError):
        await builder.compile(checkpointer=saver).ainvoke({}, config, durability="sync")
    right.side_effect = None
    right.return_value = {"right": 2}
    result = await builder.compile(
        checkpointer=InvestigationCheckpointSaver(store)
    ).ainvoke(None, config, durability="sync")
    assert result == {"left": 1, "right": 2}
    assert left.await_count == 1 and right.await_count == 2


@pytest.mark.parametrize(
    "phase", ["read_checkpoint", "write_checkpoint", "write_nodes"]
)
@pytest.mark.parametrize("own_deadline", [False, True])
async def test_checkpoint_io_is_inside_segment_deadline_and_cancellation_boundary(
    monkeypatch, phase, own_deadline
):
    store, entered, cleaned, scopes = (
        _MemoryStore(),
        asyncio.Event(),
        asyncio.Event(),
        [],
    )
    real_timeout = asyncio.timeout

    def timeout(seconds):
        scope = real_timeout(seconds)
        scopes.append(scope)
        return scope

    monkeypatch.setattr(
        execution,
        "asyncio",
        SimpleNamespace(
            timeout=timeout,
            create_task=asyncio.create_task,
            shield=asyncio.shield,
            gather=asyncio.gather,
        ),
    )

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    setattr(store, phase, blocked)
    operation = asyncio.create_task(_execute(store))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if own_deadline:
            scopes[0].reschedule(asyncio.get_running_loop().time())
        else:
            operation.cancel()
        with pytest.raises(
            InvestigationDeadlineExceeded if own_deadline else asyncio.CancelledError
        ):
            await operation
        assert cleaned.is_set()
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("empty", [False, True])
async def test_insufficient_completed_checkpoint_is_a_valid_reusable_result(empty):
    store = _MemoryStore(_run(max_search_rounds=1))
    history = AsyncMock(return_value=() if empty else (_hit(),))
    analyzer = AsyncMock(
        side_effect=lambda request: _analysis(request, needs_more=True)
    )
    result = await _execute(store, history=history, analyzer=analyzer)
    repeated = await _execute(store, history=history, analyzer=analyzer)
    assert result == repeated and result.next_step == "needs_more_evidence"
    assert history.await_count == 1 and analyzer.await_count == (0 if empty else 1)
    assert bool(result.analysis_evidence) == (not empty)


async def test_restored_final_context_is_the_actual_analysis_input_before_non_novel_refresh():
    store = _MemoryStore(_run(search_strategy="tool", tool_timeout_seconds=1.0))
    analyzer = AsyncMock(
        side_effect=lambda request: _analysis(request, needs_more=True)
    )

    async def tool(state):
        refreshed = replace(
            _hit(), content="Updated discussion after original analysis.", distance=0.01
        )
        return {
            "tool_name": "search_history",
            "tool_call_id": "refresh",
            "query": "followup cleanup refresh",
            "evidence": [],
            "history_evidence": [to_history_evidence(refreshed, REPOSITORY)],
            "source_evidence": [],
            "skipped_duplicate": False,
            "model_response": "Refreshed an existing record.",
        }

    history = AsyncMock(return_value=(_hit(),))
    result = await _execute(store, history=history, analyzer=analyzer, tool=tool)
    repeated = await _execute(store, history=history, analyzer=analyzer, tool=tool)
    assert result == repeated and result.search_round == 2
    assert analyzer.await_count == history.await_count == 1
    assert (
        result.history_evidence[0]["content"]
        == "Updated discussion after original analysis."
    )
    assert result.analysis_evidence == analyzer.call_args.args[0]["evidence"]
    assert (
        "Cancellation cleanup requires finally."
        in result.analysis_evidence[0]["content"]
    )


async def test_restored_analysis_cannot_cite_an_unreturned_record():
    store = _MemoryStore()
    await _execute(store)
    key = max(store.frames, key=lambda item: item[1])
    saver, row = InvestigationCheckpointSaver(store), store.frames[key]
    checkpoint = saver.serde.loads_typed((row.payload_type, row.payload))
    checkpoint["channel_values"]["preliminary_analysis"]["findings"][0][
        "evidence_ids"
    ] = ["history:item:999"]
    payload_type, payload = saver.serde.dumps_typed(checkpoint)
    store.frames[key] = replace(row, payload_type=payload_type, payload=payload)
    history, analyzer = AsyncMock(), AsyncMock()
    with pytest.raises(InvalidInvestigationCheckpointError):
        await _execute(store, history=history, analyzer=analyzer)
    history.assert_not_awaited()
    analyzer.assert_not_awaited()


@pytest.mark.parametrize("failed_stage", ["source", "analysis"])
async def test_source_checkpoint_restores_typed_uuid_and_reuses_committed_nodes(
    failed_stage,
):
    store = _MemoryStore(_run(source_enabled=True))
    hit = HybridSearchResult(
        index_id=INDEX_ID,
        chunk_key="c" * 64,
        repository=REPOSITORY,
        commit_sha="a" * 40,
        file_path="client.py",
        blob_sha="b" * 40,
        language="python",
        kind="code",
        symbol="cleanup",
        start_line=1,
        end_line=2,
        content="def cleanup():\n    release()",
        rrf_score=0.03,
        vector_rank=1,
        keyword_rank=None,
        cosine_distance=0.1,
        matched_in=None,
    )
    history, source = AsyncMock(return_value=(_hit(),)), AsyncMock(return_value=(hit,))

    def analyze(request):
        return InvestigationAnalysis(
            summary="History and Source evidence.",
            findings=[
                {
                    "statement": "Cleanup has supporting evidence.",
                    "evidence_ids": [
                        item["evidence_id"] for item in request["evidence"]
                    ],
                }
            ],
            hypotheses=[],
            unanswered_questions=[],
        )

    analyzer = AsyncMock(side_effect=analyze)
    failing = source if failed_stage == "source" else analyzer
    failing.side_effect = RetryableInvestigationError("Interrupted stage")

    async def invoke():
        return await execution.execute_checkpointed_investigation(
            store.run,
            store,
            history_searcher=history,
            source_searcher=source,
            analyzer=analyzer,
        )

    with pytest.raises(RetryableInvestigationError):
        await invoke()
    failing.side_effect = None if failed_stage == "source" else analyze
    result = await invoke()
    assert result.source_index_id == INDEX_ID and result.source_evidence
    assert {item["category"] for item in result.analysis_evidence} == {
        "history",
        "source",
    }
    assert history.await_count == 1
    assert source.await_count == (2 if failed_stage == "source" else 1)
    assert analyzer.await_count == (2 if failed_stage == "analysis" else 1)
    counts = history.await_count, source.await_count, analyzer.await_count
    assert await invoke() == result
    assert (history.await_count, source.await_count, analyzer.await_count) == counts
