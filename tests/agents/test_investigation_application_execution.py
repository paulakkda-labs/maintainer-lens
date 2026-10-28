"""Complete application executions with real compiled graphs and fake dependencies."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

import maintainer_lens.services.investigation_execution as application
from maintainer_lens.agents.investigation.evidence import (
    to_history_evidence,
    to_source_evidence,
)
from maintainer_lens.agents.investigation.query_planning import FollowUpQueryPlan
from maintainer_lens.agents.investigation.state import (
    InvalidInvestigationResultError,
    InvestigationState,
)
from maintainer_lens.agents.investigation_analysis import InvestigationAnalysis
from maintainer_lens.investigation_runner import (
    LeaseLostError,
    PermanentInvestigationError,
    RetryableInvestigationError,
)
from maintainer_lens.services.investigation_execution import (
    InvestigationDeadlineExceeded,
    InvestigationInput,
    InvestigationResult,
    execute_graph_investigation,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)
from maintainer_lens.services.repository_hybrid_search import HybridSearchResult

pytestmark = pytest.mark.anyio

REPOSITORY = "encode/httpx"
INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
OTHER_INDEX_ID = UUID("22345678-1234-4234-8234-123456789abc")
FOLLOW_UP = "How does cancellation release pooled connections?"
ASYNC_PHASES = (
    "history",
    "source",
    "analysis",
    "planner",
    "tool",
    "follow_up_history",
    "follow_up_source",
    "reanalysis",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _request(**changes) -> InvestigationInput:
    return InvestigationInput.model_validate(
        {
            "repository": REPOSITORY,
            "issue_number": 9000,
            "issue_title": "Connection cancellation",
            "issue_body": "Connections may remain active after cancellation.",
            "response_language": "en",
            **changes,
        }
    )


def _history_hit(number: int = 3782) -> RepositoryHistorySemanticHit:
    return RepositoryHistorySemanticHit(
        repository=REPOSITORY,
        number=number,
        source_type="item",
        comment_id=None,
        title=f"Historical issue #{number}",
        content=f"Historical cancellation discussion #{number}. " + "x" * 2000,
        source_url=f"https://github.com/{REPOSITORY}/issues/{number}",
        source_updated_at=datetime(2026, 10, 25, tzinfo=UTC),
        distance=0.12,
    )


def _source_hit(chunk_key: str = "chunk-001") -> HybridSearchResult:
    return HybridSearchResult(
        index_id=INDEX_ID,
        chunk_key=chunk_key,
        repository=REPOSITORY,
        commit_sha="a" * 40,
        blob_sha="b" * 40,
        file_path="httpx/client request.py",
        language="python",
        kind="code",
        symbol="send",
        start_line=100,
        end_line=120,
        content="async def send(self, request):\n" + "x" * 2000,
        rrf_score=0.03,
        vector_rank=1,
        keyword_rank=1,
        cosine_distance=0.12,
        matched_in="content",
    )


def _analysis(
    evidence_id: str = "history:item:3782",
    *,
    needs_more: bool = False,
) -> InvestigationAnalysis:
    return InvestigationAnalysis(
        summary="Preliminary investigation completed.",
        findings=[
            {
                "statement": "A relevant record was retrieved.",
                "evidence_ids": [evidence_id],
            }
        ],
        hypotheses=[],
        unanswered_questions=[FOLLOW_UP] if needs_more else [],
    )


def _tool_result(*, hits=None, skipped: bool = False) -> dict:
    return {
        "tool_name": "search_history",
        "tool_call_id": "call-123",
        "query": "AsyncClient cancellation pool release",
        "evidence": [],
        "history_evidence": [
            to_history_evidence(hit, REPOSITORY)
            for hit in (hits if hits is not None else [_history_hit(1171)])
        ],
        "source_evidence": [],
        "skipped_duplicate": skipped,
        "model_response": "Retrieved another discussion.",
    }


async def _execute(request: InvestigationInput | None = None, **options):
    dependencies = {
        "history_searcher": AsyncMock(return_value=(_history_hit(),)),
        "analyzer": AsyncMock(return_value=_analysis()),
        "timeout_seconds": 5.0,
        **options,
    }
    return await execute_graph_investigation(
        request if request is not None else _request(), **dependencies
    )


async def test_result_preserves_identity_full_records_and_context() -> None:
    history = tuple(_history_hit(number) for number in range(1, 8))
    source = [_source_hit(f"chunk-{number}") for number in range(1, 8)]
    captured = []
    calls = []

    async def history_searcher(repository, query):
        calls.append("history")
        assert repository == REPOSITORY
        assert "Connection cancellation" in query
        return history

    async def source_searcher(repository, index_id, query):
        calls.append("source")
        assert repository == REPOSITORY
        assert index_id == INDEX_ID
        return source

    async def analyzer(request):
        calls.append("analysis")
        captured.append(deepcopy(request))
        result = _analysis("history:item:1")
        result.findings[0].evidence_ids.append(f"source:{INDEX_ID}:chunk-1")
        return result

    request = _request(source_index_id=str(INDEX_ID), response_language="zh")
    result = await _execute(
        request,
        history_searcher=history_searcher,
        source_searcher=source_searcher,
        analyzer=analyzer,
    )

    assert isinstance(result, InvestigationResult)
    assert isinstance(result.preliminary_analysis, InvestigationAnalysis)
    for key, expected in request.model_dump().items():
        assert getattr(result, key) == expected
    assert calls == ["history", "source", "analysis"]
    assert result.next_step == "draft_preliminary_report"
    assert result.search_round == 1
    assert "no declared gaps" in result.routing_reason
    assert len(result.history_evidence) == len(result.source_evidence) == 7
    assert result.history_evidence[0] == to_history_evidence(history[0], REPOSITORY)
    assert result.source_evidence[0] == to_source_evidence(
        source[0], REPOSITORY, INDEX_ID
    )
    assert result.analysis_evidence == captured[0]["evidence"]
    assert len(result.analysis_evidence) == 10
    assert captured[0]["response_language"] == "zh"
    assert result.analysis_evidence[0]["content"] == (
        f"{history[0].title}\n{history[0].content[:1600]}"
    )
    assert result.analysis_evidence[5]["content"] == (
        f"{source[0].file_path}:100-120\n{source[0].content[:1600]}"
    )
    assert "searched_queries" not in result.model_dump()
    assert "no_new_evidence" not in result.model_dump()


@pytest.mark.parametrize("gap", ["question", "hypothesis", "no_findings"])
async def test_insufficient_evidence_is_a_legal_result(gap) -> None:
    analysis = _analysis()
    if gap == "question":
        analysis.unanswered_questions = [FOLLOW_UP]
    elif gap == "hypothesis":
        analysis.hypotheses = ["Cancellation may interrupt cleanup."]
    else:
        analysis.findings = []

    result = await _execute(analyzer=AsyncMock(return_value=analysis))

    assert result.next_step == "needs_more_evidence"
    assert result.preliminary_analysis == analysis
    assert result.search_round == 1


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_empty_evidence_returns_local_analysis_without_calling_analyzer(language):
    analyzer = AsyncMock()
    result = await _execute(
        _request(response_language=language, source_index_id=INDEX_ID),
        history_searcher=AsyncMock(return_value=()),
        source_searcher=AsyncMock(return_value=[]),
        analyzer=analyzer,
        max_search_rounds=3,
    )

    analyzer.assert_not_awaited()
    assert result.next_step == "needs_more_evidence"
    assert (
        result.history_evidence
        == result.source_evidence
        == result.analysis_evidence
        == []
    )
    assert result.preliminary_analysis.findings == []
    assert result.preliminary_analysis.unanswered_questions
    assert result.search_round == 1
    assert ("当前" in result.preliminary_analysis.summary) == (language == "zh")


async def test_each_execution_builds_fresh_state() -> None:
    request = _request(source_index_id=INDEX_ID, issue_body=None)
    history = AsyncMock(side_effect=[(_history_hit(),), ()])
    analyzer = AsyncMock(return_value=_analysis())

    first = await _execute(request, history_searcher=history, analyzer=analyzer)
    second = await _execute(request, history_searcher=history, analyzer=analyzer)

    assert first.next_step == "draft_preliminary_report"
    assert second.next_step == "needs_more_evidence"
    assert second.analysis_evidence == second.history_evidence == []
    assert first.source_index_id == second.source_index_id == INDEX_ID
    assert first.issue_body is second.issue_body is None
    analyzer.assert_awaited_once()


@pytest.mark.parametrize("strategy", ["deterministic", "planner", "tool"])
async def test_follow_up_merges_evidence_and_replaces_analysis_context(
    strategy,
) -> None:
    history = AsyncMock(side_effect=[(_history_hit(),), (_history_hit(1171),)])
    source = AsyncMock(return_value=[_source_hit()])
    analyzer = AsyncMock(
        side_effect=[_analysis(needs_more=True), _analysis("history:item:1171")]
    )
    options = {}
    if strategy == "planner":
        options["query_planner"] = AsyncMock(
            return_value=FollowUpQueryPlan(
                query="AsyncClient cleanup", reason="Find cleanup"
            )
        )
    elif strategy == "tool":
        options["tool_search_runner"] = AsyncMock(return_value=_tool_result())

    result = await _execute(
        _request(source_index_id=INDEX_ID),
        history_searcher=history,
        source_searcher=source,
        analyzer=analyzer,
        max_search_rounds=2,
        **options,
    )

    assert result.search_round == 2
    assert result.next_step == "draft_preliminary_report"
    assert [hit["number"] for hit in result.history_evidence] == [1171, 3782]
    assert result.analysis_evidence == analyzer.await_args_list[1].args[0]["evidence"]
    assert result.analysis_evidence[0]["evidence_id"] == "history:item:1171"
    assert analyzer.await_count == 2
    # A selected History tool goes straight to analysis, without fixed-node reruns.
    assert history.await_count == source.await_count == (1 if strategy == "tool" else 2)


@pytest.mark.parametrize("strategy", ["planner", "tool"])
async def test_skipped_follow_up_is_a_legal_terminal_result(strategy) -> None:
    if strategy == "planner":
        options = {
            "query_planner": AsyncMock(
                return_value=FollowUpQueryPlan(
                    query=None, reason="No useful new query."
                )
            )
        }
    else:
        options = {
            "tool_search_runner": AsyncMock(return_value=_tool_result(skipped=True))
        }
    analyzer = AsyncMock(return_value=_analysis(needs_more=True))
    result = await _execute(analyzer=analyzer, max_search_rounds=2, **options)

    assert result.next_step == "needs_more_evidence"
    assert result.search_round == 1
    assert result.analysis_evidence == analyzer.await_args.args[0]["evidence"]
    analyzer.assert_awaited_once()


async def test_three_round_budget_stops_without_a_fourth_search() -> None:
    follow_up_queries = (
        "Which cancellation path releases the connection?",
        "Does the connection pool shield asynchronous cleanup?",
        "How does repeated cancellation affect the cleanup task?",
    )
    analyses = []
    for number, query in enumerate(follow_up_queries, start=1):
        analysis = _analysis(f"history:item:{number}", needs_more=True)
        analysis.unanswered_questions = [query]
        analyses.append(analysis)

    history = AsyncMock(side_effect=[(_history_hit(n),) for n in (1, 2, 3)])
    analyzer = AsyncMock(side_effect=analyses)
    result = await _execute(
        history_searcher=history, analyzer=analyzer, max_search_rounds=3
    )

    assert result.next_step == "needs_more_evidence"
    assert result.search_round == history.await_count == analyzer.await_count == 3
    assert [hit["number"] for hit in result.history_evidence] == [3, 2, 1]
    searched_queries = [call.args[1] for call in history.await_args_list]
    assert searched_queries[1:] == list(follow_up_queries[:2])
    assert follow_up_queries[2] not in searched_queries
    assert result.preliminary_analysis.unanswered_questions == [follow_up_queries[2]]


@pytest.mark.parametrize("strategy", ["fixed", "tool"])
async def test_no_novelty_keeps_actual_context_despite_reranking_and_updated_text(
    strategy,
):
    initial = tuple(_history_hit(n) for n in range(1, 8))
    reranked = (
        initial[6],
        initial[5],
        replace(initial[0], content="New version of the same historical object."),
    )
    history = AsyncMock(side_effect=[initial, reranked])
    analyzer = AsyncMock(return_value=_analysis("history:item:5", needs_more=True))
    options = {}
    if strategy == "tool":
        options["tool_search_runner"] = AsyncMock(
            return_value=_tool_result(hits=reranked)
        )

    result = await _execute(
        history_searcher=history, analyzer=analyzer, max_search_rounds=2, **options
    )

    assert result.next_step == "needs_more_evidence"
    assert result.search_round == 2
    assert "no new evidence" in result.routing_reason
    analyzer.assert_awaited_once()
    assert [hit["number"] for hit in result.history_evidence[:5]] == [7, 6, 1, 2, 3]
    assert [item["evidence_id"] for item in result.analysis_evidence] == [
        f"history:item:{n}" for n in range(1, 6)
    ]
    assert result.analysis_evidence == analyzer.await_args.args[0]["evidence"]
    assert result.analysis_evidence[0]["content"] == (
        f"{initial[0].title}\n{initial[0].content[:1600]}"
    )
    assert result.history_evidence[2]["content"] == reranked[2].content


async def test_analyzer_cannot_mutate_saved_context_through_its_request() -> None:
    async def analyzer(request):
        request["evidence"][0]["content"] = "Changed by analyzer"
        request["evidence"][0]["evidence_id"] = "history:item:999999"
        return _analysis()

    result = await _execute(analyzer=analyzer)

    assert result.analysis_evidence[0]["evidence_id"] == "history:item:3782"
    assert "Changed by analyzer" not in result.analysis_evidence[0]["content"]


@pytest.mark.parametrize("evidence_id", ["history:item:999999", "history:item:6"])
async def test_analysis_cannot_cite_fabricated_or_unsupplied_evidence(
    evidence_id,
) -> None:
    with pytest.raises(InvalidInvestigationResultError, match="not supplied"):
        await _execute(
            history_searcher=AsyncMock(
                return_value=tuple(_history_hit(n) for n in range(1, 8))
            ),
            analyzer=AsyncMock(return_value=_analysis(evidence_id)),
        )


@pytest.mark.parametrize("invalid", ["wrong_type", "invalid_model"])
async def test_invalid_analyzer_return_uses_result_error(invalid) -> None:
    output = _analysis().model_dump()
    if invalid == "invalid_model":
        output = _analysis().model_copy(update={"summary": ""})

    with pytest.raises(InvalidInvestigationResultError):
        await _execute(analyzer=AsyncMock(return_value=output))


def _phased_dependencies(blocked_phase, block):
    """Reach every async phase through the actual production graph composition."""
    counts = {"history": 0, "source": 0, "analysis": 0}

    async def phase(name):
        if name == blocked_phase:
            await block()

    async def history(repository, query):
        counts["history"] += 1
        await phase("history" if counts["history"] == 1 else "follow_up_history")
        return (_history_hit(3782 if counts["history"] == 1 else 1171),)

    async def source(repository, index_id, query):
        counts["source"] += 1
        await phase("source" if counts["source"] == 1 else "follow_up_source")
        return [_source_hit(f"chunk-{counts['source']}")]

    async def analyze(request):
        counts["analysis"] += 1
        await phase("analysis" if counts["analysis"] == 1 else "reanalysis")
        return _analysis(needs_more=counts["analysis"] == 1)

    async def planner(request):
        await phase("planner")
        return FollowUpQueryPlan(query="AsyncClient cleanup", reason="Find cleanup.")

    async def tool(state):
        await phase("tool")
        return _tool_result()

    options = {
        "history_searcher": history,
        "source_searcher": source,
        "analyzer": analyze,
        "max_search_rounds": 2,
    }
    if blocked_phase == "planner":
        options["query_planner"] = planner
    elif blocked_phase == "tool":
        options["tool_search_runner"] = tool
    return options


@pytest.mark.parametrize("phase", ASYNC_PHASES)
async def test_whole_graph_deadline_cancels_every_async_phase_and_waits_for_cleanup(
    phase,
):
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def block():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = asyncio.create_task(
        _execute(
            _request(source_index_id=INDEX_ID),
            timeout_seconds=0.5,
            **_phased_dependencies(phase, block),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        with pytest.raises(InvestigationDeadlineExceeded):
            await asyncio.wait_for(task, timeout=2)
        assert cleaned.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_one_deadline_covers_cumulative_time_across_nodes() -> None:
    entered_source = asyncio.Event()
    source_cleaned = asyncio.Event()

    async def history(repository, query):
        await asyncio.sleep(0.15)
        return (_history_hit(),)

    async def source(repository, index_id, query):
        entered_source.set()
        try:
            await asyncio.sleep(0.15)
            return [_source_hit()]
        finally:
            source_cleaned.set()

    with pytest.raises(InvestigationDeadlineExceeded):
        await _execute(
            _request(source_index_id=INDEX_ID),
            history_searcher=history,
            source_searcher=source,
            timeout_seconds=0.25,
        )

    assert entered_source.is_set()
    assert source_cleaned.is_set()


@pytest.mark.parametrize("phase", ASYNC_PHASES)
async def test_external_cancellation_propagates_after_dependency_cleanup(phase):
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def block():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = asyncio.create_task(
        _execute(
            _request(source_index_id=INDEX_ID),
            **_phased_dependencies(phase, block),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel("external cancellation")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert cleaned.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_swallowed_deadline_cancellation_cannot_return_success() -> None:
    swallowed = asyncio.Event()

    async def history(repository, query):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            return ()

    with pytest.raises(InvestigationDeadlineExceeded):
        await _execute(history_searcher=history, timeout_seconds=0.1)
    assert swallowed.is_set()


@pytest.mark.parametrize("phase", ["history", "analysis", "tool"])
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("Upstream provider timeout"),
        ValueError("Dependency input rejected"),
        RuntimeError("Provider unavailable"),
        LeaseLostError("Claim was replaced"),
        RetryableInvestigationError("Temporary business failure"),
        PermanentInvestigationError("Permanent business failure"),
        ExceptionGroup(
            "mixed",
            [LeaseLostError("Lost"), ExceptionGroup("nested", [ValueError("Invalid")])],
        ),
        BaseExceptionGroup(
            "cancelled group",
            [asyncio.CancelledError(), RuntimeError("Cleanup failed")],
        ),
    ],
    ids=[
        "upstream_timeout",
        "value",
        "runtime",
        "lease",
        "retryable",
        "permanent",
        "group",
        "base_group",
    ],
)
async def test_dependency_errors_retain_identity_type_and_group_structure(phase, error):
    options = {"max_search_rounds": 2}
    if phase == "history":
        options["history_searcher"] = AsyncMock(side_effect=error)
    elif phase == "analysis":
        options["analyzer"] = AsyncMock(side_effect=error)
    else:
        options["analyzer"] = AsyncMock(return_value=_analysis(needs_more=True))
        options["tool_search_runner"] = AsyncMock(side_effect=error)

    with pytest.raises(type(error)) as caught:
        await _execute(**options)
    assert caught.value is error


def _terminal_updates() -> dict:
    """A valid terminal fixture, independently of production routing."""
    hit = _history_hit()
    record = to_history_evidence(hit, REPOSITORY)
    return {
        "history_evidence": [record],
        "source_evidence": [],
        "preliminary_analysis": _analysis().model_dump(),
        "analysis_evidence": [
            {
                "evidence_id": "history:item:3782",
                "category": "history",
                "source_url": hit.source_url,
                "content": f"{hit.title}\n{hit.content[:1600]}",
            }
        ],
        "search_round": 1,
        "no_new_evidence": False,
        "next_step": "draft_preliminary_report",
        "routing_reason": "The preliminary analysis has cited findings and no declared gaps.",
    }


def _install_terminal_graph(monkeypatch, updates) -> None:
    """Inject a real compiled graph solely to exercise malformed terminal states."""
    builder = StateGraph(InvestigationState)
    builder.add_node("terminal_fixture", lambda state: deepcopy(updates))
    builder.add_edge(START, "terminal_fixture")
    builder.add_edge("terminal_fixture", END)
    graph = builder.compile()
    monkeypatch.setattr(
        application, "build_investigation_graph", lambda *a, **kw: graph
    )


@pytest.mark.parametrize(
    "case",
    [
        "missing_analysis",
        "missing_context",
        "missing_reason",
        "invalid_analysis",
        "invalid_terminal",
        "round_bool",
        "round_over_budget",
        "blank_reason",
        "wrong_repository",
        "wrong_issue",
        "wrong_title",
        "wrong_body",
        "wrong_language",
        "wrong_index",
        "invalid_novelty_flag",
        "wrong_history_repository",
        "self_history",
        "wrong_history_url",
        "history_without_timezone",
        "duplicate_history",
        "malformed_record",
        "unresolved_context",
        "wrong_context_url",
        "wrong_context_category",
        "duplicate_context",
        "finding_outside_context",
        "ready_with_gaps",
        "needs_more_despite_ready",
        "ready_with_no_novelty",
    ],
)
async def test_rejects_invalid_terminal_data_from_compiled_graph(monkeypatch, case):
    updates = _terminal_updates()
    if case.startswith("missing_"):
        updates.pop(
            {
                "missing_analysis": "preliminary_analysis",
                "missing_context": "analysis_evidence",
                "missing_reason": "routing_reason",
            }[case]
        )
    elif case == "invalid_analysis":
        updates["preliminary_analysis"]["summary"] = ""
    elif case == "invalid_terminal":
        updates["next_step"] = "succeeded"
    elif case == "round_bool":
        updates["search_round"] = True
    elif case == "round_over_budget":
        updates["search_round"] = 2
    elif case == "blank_reason":
        updates["routing_reason"] = " "
    elif case in (
        "wrong_repository",
        "wrong_issue",
        "wrong_title",
        "wrong_body",
        "wrong_language",
        "wrong_index",
    ):
        key, value = {
            "wrong_repository": ("repository", "another/repository"),
            "wrong_issue": ("issue_number", 1),
            "wrong_title": ("issue_title", "Changed"),
            "wrong_body": ("issue_body", None),
            "wrong_language": ("response_language", "zh"),
            "wrong_index": ("source_index_id", str(INDEX_ID)),
        }[case]
        updates[key] = value
    elif case == "invalid_novelty_flag":
        updates["no_new_evidence"] = "false"
    elif case == "wrong_history_repository":
        updates["history_evidence"][0]["repository"] = "another/repository"
    elif case == "self_history":
        updates["history_evidence"][0]["number"] = 9000
        updates["history_evidence"][0]["source_url"] = (
            f"https://github.com/{REPOSITORY}/issues/9000"
        )
    elif case == "wrong_history_url":
        updates["history_evidence"][0]["source_url"] = "https://evil.example/"
    elif case == "history_without_timezone":
        updates["history_evidence"][0]["source_updated_at"] = "2026-10-25T00:00:00"
    elif case == "duplicate_history":
        updates["history_evidence"] *= 2
    elif case == "malformed_record":
        updates["history_evidence"][0].pop("content")
    elif case == "unresolved_context":
        updates["analysis_evidence"][0]["evidence_id"] = "history:item:999999"
    elif case == "wrong_context_url":
        updates["analysis_evidence"][0]["source_url"] = "https://evil.example/"
    elif case == "wrong_context_category":
        updates["analysis_evidence"][0]["category"] = "source"
    elif case == "duplicate_context":
        updates["analysis_evidence"] *= 2
    elif case == "finding_outside_context":
        updates["history_evidence"].append(
            to_history_evidence(_history_hit(1171), REPOSITORY)
        )
        updates["preliminary_analysis"]["findings"][0]["evidence_ids"] = [
            "history:item:1171"
        ]
    elif case == "ready_with_gaps":
        updates["preliminary_analysis"]["hypotheses"] = ["Unverified"]
    elif case == "needs_more_despite_ready":
        updates["next_step"] = "needs_more_evidence"
    elif case == "ready_with_no_novelty":
        updates["no_new_evidence"] = True
    else:
        raise AssertionError(case)
    _install_terminal_graph(monkeypatch, updates)

    with pytest.raises(InvalidInvestigationResultError) as caught:
        await _execute()
    assert caught.value.__cause__ is not None


@pytest.mark.parametrize(
    "case",
    [
        "missing_index",
        "wrong_index",
        "wrong_repository",
        "wrong_url",
        "wrong_sha",
        "wrong_lines",
        "duplicate",
    ],
)
async def test_final_validation_checks_all_source_provenance(monkeypatch, case):
    updates = _terminal_updates()
    source = to_source_evidence(_source_hit(), REPOSITORY, INDEX_ID)
    updates["source_evidence"] = [source]
    if case == "wrong_index":
        source["index_id"] = str(OTHER_INDEX_ID)
    elif case == "wrong_repository":
        source["repository"] = "another/repository"
    elif case == "wrong_url":
        source["source_url"] = "https://evil.example/"
    elif case == "wrong_sha":
        source["commit_sha"] = "not-a-sha"
    elif case == "wrong_lines":
        source["start_line"] = 0
    elif case == "duplicate":
        updates["source_evidence"] *= 2
    _install_terminal_graph(monkeypatch, updates)
    request = (
        _request() if case == "missing_index" else _request(source_index_id=INDEX_ID)
    )

    with pytest.raises(InvalidInvestigationResultError):
        await _execute(request)


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": ""},
        {"repository": "owner/../repo"},
        {"issue_number": True},
        {"issue_number": 0},
        {"issue_number": "1"},
        {"issue_title": " "},
        {"issue_title": 12},
        {"issue_body": {}},
        {"response_language": "fr"},
        {"source_index_id": "invalid-uuid"},
        {"history_evidence": []},
        {"source_evidence": []},
        {"preliminary_analysis": {}},
        {"analysis_evidence": []},
        {"search_round": 1},
        {"next_step": "draft_preliminary_report"},
    ],
)
def test_input_rejects_invalid_identity_and_internal_state(changes) -> None:
    with pytest.raises(ValidationError):
        _request(**changes)


@pytest.mark.parametrize(
    "field",
    ["repository", "issue_number", "issue_title", "issue_body", "response_language"],
)
def test_input_requires_each_request_field(field) -> None:
    payload = _request().model_dump()
    payload.pop(field)
    with pytest.raises(ValidationError):
        InvestigationInput.model_validate(payload)


@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -1,
        True,
        False,
        None,
        "120",
        float("nan"),
        float("inf"),
        -float("inf"),
        10**1000,
    ],
)
async def test_invalid_timeout_fails_before_dependencies(timeout) -> None:
    history = AsyncMock()
    analyzer = AsyncMock()
    with pytest.raises(ValueError, match="timeout_seconds"):
        await _execute(
            history_searcher=history, analyzer=analyzer, timeout_seconds=timeout
        )
    history.assert_not_awaited()
    analyzer.assert_not_awaited()


@pytest.mark.parametrize("rounds", [0, 4, True, 1.5, "2", None])
async def test_invalid_round_budget_fails_before_dependencies(rounds) -> None:
    history = AsyncMock()
    analyzer = AsyncMock()
    with pytest.raises(ValueError, match="max_search_rounds"):
        await _execute(
            history_searcher=history, analyzer=analyzer, max_search_rounds=rounds
        )
    history.assert_not_awaited()
    analyzer.assert_not_awaited()


@pytest.mark.parametrize(
    "options",
    [
        {"history_searcher": None},
        {"analyzer": None},
        {"source_searcher": 1},
        {"query_planner": 1},
        {"tool_search_runner": 1},
        {"source_searcher": AsyncMock()},
        {"query_planner": AsyncMock()},
        {"tool_search_runner": AsyncMock()},
        {
            "query_planner": AsyncMock(),
            "tool_search_runner": AsyncMock(),
            "max_search_rounds": 2,
        },
    ],
)
async def test_invalid_configuration_fails_before_dependency_execution(options) -> None:
    history = AsyncMock()
    analyzer = AsyncMock()
    with pytest.raises(ValueError):
        await _execute(**{"history_searcher": history, "analyzer": analyzer, **options})
    for dependency in (history, analyzer, *options.values()):
        if isinstance(dependency, AsyncMock):
            dependency.assert_not_awaited()


@pytest.mark.parametrize(
    "case", ["raw_state", "bypassed_input_validation", "injected_state"]
)
async def test_application_revalidates_input_before_dependency_execution(case) -> None:
    request = _request()
    if case == "raw_state":
        request = request.model_dump()
    elif case == "bypassed_input_validation":
        request = request.model_copy(update={"issue_title": " "})
    else:
        request = request.model_copy(update={"history_evidence": []})
    history = AsyncMock()
    analyzer = AsyncMock()

    with pytest.raises(ValueError):
        await _execute(request, history_searcher=history, analyzer=analyzer)
    history.assert_not_awaited()
    analyzer.assert_not_awaited()
