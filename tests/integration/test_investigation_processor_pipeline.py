"""Processor regressions with real compiled Graph and opt-in PostgreSQL/Runner.

GitHub uses MockTransport and retrieval/analyzers are fake. No broker or provider
is contacted, no migration is applied, and shared DB fixtures clean isolated tasks.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, func, update

import maintainer_lens.investigation_runner as runner
import maintainer_lens.services.investigation_processor as processor_module
import maintainer_lens.services.issue_investigator as issue_module
from maintainer_lens.agents.investigation.query_planning import FollowUpQueryPlan
from maintainer_lens.agents.investigation_analysis import InvestigationAnalysis
from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    get_claimed_investigation_identity,
)
from maintainer_lens.db.issue_snapshots import IssueSnapshotData
from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.services.investigation_execution import (
    InvestigationDeadlineExceeded,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)
from maintainer_lens.services.repository_hybrid_search import HybridSearchResult

pytestmark = pytest.mark.anyio
requires_postgres = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration tests are disabled",
)
REPOSITORY = "fastapi/fastapi"
INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _issue_payload() -> dict:
    return {
        "number": 123,
        "title": "Cancellation cleanup",
        "body": None,
        "state": "open",
        "html_url": f"https://github.com/{REPOSITORY}/issues/123",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-28T00:00:00Z",
    }


def _history_hit() -> RepositoryHistorySemanticHit:
    return RepositoryHistorySemanticHit(
        repository=REPOSITORY,
        number=124,
        source_type="item",
        comment_id=None,
        title="Historical cleanup discussion",
        content="Cancellation may interrupt asynchronous connection cleanup.",
        source_url=f"https://github.com/{REPOSITORY}/issues/124",
        source_updated_at=datetime(2026, 10, 28, tzinfo=UTC),
        distance=0.1,
    )


def _source_hit() -> HybridSearchResult:
    return HybridSearchResult(
        index_id=INDEX_ID,
        chunk_key="c" * 64,
        repository=REPOSITORY,
        commit_sha="a" * 40,
        blob_sha="b" * 40,
        file_path="src/client.py",
        language="python",
        kind="code",
        symbol="cleanup",
        start_line=2,
        end_line=3,
        content="async def cleanup():\n    await release()",
        rrf_score=0.03,
        vector_rank=1,
        keyword_rank=None,
        cosine_distance=0.1,
        matched_in=None,
    )


def _analysis(*, needs_more=False, evidence_id="history:item:124"):
    return InvestigationAnalysis(
        summary="Relevant historical evidence was retrieved.",
        findings=[
            {"statement": "Cleanup is discussed.", "evidence_ids": [evidence_id]}
        ],
        hypotheses=[],
        unanswered_questions=["Which cleanup path runs?"] if needs_more else [],
    )


class _FakeDatabase:
    """Expose resource/commit ordering without pretending to verify PostgreSQL."""

    def __init__(self):
        self.sessions = 0
        self.open_sessions = 0
        self.transactions = 0
        self.commits = []
        self.rollbacks = []
        self.fail_commit = None
        self.commit_error = RuntimeError("Commit failed")

    @asynccontextmanager
    async def __call__(self):
        self.sessions += 1
        number = self.sessions
        self.open_sessions += 1
        try:
            yield SimpleNamespace(begin=lambda: self._transaction(number))
        finally:
            self.open_sessions -= 1

    @asynccontextmanager
    async def _transaction(self, number):
        self.transactions += 1
        try:
            yield
        except BaseException:
            self.rollbacks.append(number)
            raise
        else:
            if number == self.fail_commit:
                raise self.commit_error
            self.commits.append(number)
        finally:
            self.transactions -= 1


@pytest.fixture
def offline(monkeypatch):
    db = _FakeDatabase()
    requests = []
    snapshots = []
    results = []
    identity_languages = []

    async def identity(
        session,
        *,
        task_id,
        claim_token,
        auto_prepare_knowledge=False,
        response_language="zh",
    ):
        assert db.transactions == 1
        assert task_id == "task-001"
        assert claim_token == "token-001"
        assert auto_prepare_knowledge is False
        assert response_language in ("en", "zh")
        identity_languages.append(response_language)
        return REPOSITORY, 123, None

    async def snapshot(session, **kwargs):
        assert db.transactions == 1
        snapshots.append(kwargs["snapshot"])
        return True

    async def result(session, **kwargs):
        assert db.transactions == 1
        assert db.commits == [1, 2]
        results.append(kwargs["result"])
        return True

    def transport(request):
        assert db.open_sessions == db.transactions == 0
        requests.append(request.url.path)
        if request.url.path == f"/repos/{REPOSITORY}":
            return httpx.Response(200, json={"private": False})
        assert request.url.path == f"/repos/{REPOSITORY}/issues/123"
        return httpx.Response(200, json=_issue_payload())

    read_identity = AsyncMock(side_effect=identity)
    write_snapshot = AsyncMock(side_effect=snapshot)
    write_result = AsyncMock(side_effect=result)
    monkeypatch.setattr(
        processor_module, "get_claimed_investigation_identity", read_identity
    )
    monkeypatch.setattr(issue_module, "upsert_issue_snapshot", write_snapshot)
    monkeypatch.setattr(processor_module, "upsert_investigation_result", write_result)
    return SimpleNamespace(
        db=db,
        requests=requests,
        snapshots=snapshots,
        results=results,
        identity=read_identity,
        identity_languages=identity_languages,
        snapshot=write_snapshot,
        result=write_result,
        transport=httpx.MockTransport(transport),
    )


async def _invoke(factory, client, **changes):
    options = {
        "http_client": client,
        "session_factory": factory,
        "task_id": "task-001",
        "claim_token": "token-001",
        "repository": REPOSITORY,
        "issue_number": 123,
        "response_language": "en",
        "history_searcher": AsyncMock(return_value=(_history_hit(),)),
        "analyzer": AsyncMock(return_value=_analysis()),
        "timeout_seconds": 5.0,
        **changes,
    }
    return await processor_module.process_graph_investigation(**options)


async def test_pipeline_commits_snapshot_before_real_graph_and_result_before_return(
    offline,
):
    h = offline

    async def history(repository, query):
        assert h.db.commits == [1, 2]
        assert h.db.open_sessions == h.db.transactions == 0
        assert repository == REPOSITORY
        assert "Cancellation cleanup" in query
        return (_history_hit(),)

    async def analyze(request):
        assert h.db.open_sessions == 0
        assert request["issue_title"] == _issue_payload()["title"]
        assert request["issue_body"] is None
        assert request["response_language"] == "zh"
        assert "claim_token" not in request
        return _analysis()

    async with httpx.AsyncClient(transport=h.transport) as client:
        assert (
            await _invoke(
                h.db,
                client,
                history_searcher=history,
                analyzer=analyze,
                response_language="zh",
            )
            is None
        )
        assert not client.is_closed
    assert h.db.commits == [1, 2, 3]
    assert h.db.open_sessions == h.db.transactions == 0
    assert isinstance(h.snapshots[0], IssueSnapshotData)
    assert h.results[0].issue_title == h.snapshots[0].title
    assert h.results[0].next_step == "draft_preliminary_report"
    assert h.requests == [f"/repos/{REPOSITORY}", f"/repos/{REPOSITORY}/issues/123"]


async def test_issue_service_returns_data_only_after_its_commit(offline):
    h = offline
    async with httpx.AsyncClient(transport=h.transport) as client:
        snapshot = await issue_module.investigate_issue(
            http_client=client,
            session_factory=h.db,
            task_id="task-001",
            claim_token="token-001",
            repository=REPOSITORY,
            issue_number=123,
        )
    assert snapshot is h.snapshots[0]
    assert h.db.commits == [1]
    assert h.db.open_sessions == h.db.transactions == 0


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_preflight_and_graph_result_keep_the_requested_language(
    offline, language
):
    async with httpx.AsyncClient(transport=offline.transport) as client:
        await _invoke(offline.db, client, response_language=language)
    assert offline.identity_languages == [language]
    assert offline.results[0].response_language == language
    assert offline.db.commits == [1, 2, 3]
    assert offline.db.open_sessions == offline.db.transactions == 0


@pytest.mark.parametrize(
    "changes", [{"repository": "another/repo"}, {"issue_number": 999}]
)
async def test_identity_mismatch_stops_before_external_dependencies(offline, changes):
    h = offline
    history = AsyncMock()
    analyzer = AsyncMock()
    async with httpx.AsyncClient(transport=h.transport) as client:
        with pytest.raises(runner.PermanentInvestigationError, match="task identity"):
            await _invoke(
                h.db, client, history_searcher=history, analyzer=analyzer, **changes
            )
    assert h.requests == []
    assert h.db.rollbacks == [1]
    h.snapshot.assert_not_awaited()
    h.result.assert_not_awaited()
    history.assert_not_awaited()
    analyzer.assert_not_awaited()


async def test_invalid_identity_types_fail_before_database_or_network(offline):
    async with httpx.AsyncClient(transport=offline.transport) as client:
        with pytest.raises(ValueError):
            await _invoke(offline.db, client, issue_number=True)
    assert offline.db.sessions == 0
    assert offline.requests == []


async def test_inactive_claim_stops_before_snapshot_or_graph(offline):
    h = offline
    h.identity.side_effect = None
    h.identity.return_value = None
    async with httpx.AsyncClient(transport=h.transport) as client:
        with pytest.raises(runner.LeaseLostError):
            await _invoke(h.db, client)
    assert h.requests == []
    h.snapshot.assert_not_awaited()
    h.result.assert_not_awaited()


@pytest.mark.parametrize(
    "stage", ["snapshot_write", "snapshot_commit", "result_write", "result_commit"]
)
async def test_write_and_commit_failures_cannot_return_success(offline, stage):
    h = offline
    error = RuntimeError(stage)
    if stage.endswith("commit"):
        h.db.fail_commit = 2 if stage == "snapshot_commit" else 3
        h.db.commit_error = error
    elif stage == "snapshot_write":
        h.snapshot.side_effect = error
    else:
        h.result.side_effect = error
    analyzer = AsyncMock(return_value=_analysis())
    async with httpx.AsyncClient(transport=h.transport) as client:
        with pytest.raises(RuntimeError) as caught:
            await _invoke(h.db, client, analyzer=analyzer)
    assert caught.value is error
    assert h.db.open_sessions == h.db.transactions == 0
    assert 3 not in h.db.commits
    if stage.startswith("snapshot"):
        analyzer.assert_not_awaited()
        h.result.assert_not_awaited()


@pytest.mark.parametrize("stage", ["snapshot", "result"])
async def test_false_guarded_write_becomes_lease_lost(offline, stage):
    h = offline
    writer = h.snapshot if stage == "snapshot" else h.result
    writer.side_effect = None
    writer.return_value = False
    async with httpx.AsyncClient(transport=h.transport) as client:
        with pytest.raises(runner.LeaseLostError):
            await _invoke(h.db, client)
    assert (2 if stage == "snapshot" else 3) in h.db.rollbacks
    assert 3 not in h.db.commits


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Invalid analyzer data"),
        TimeoutError("Upstream timeout"),
        runner.LeaseLostError("Lost claim"),
        ExceptionGroup("mixed", [runner.LeaseLostError("lost"), RuntimeError("other")]),
    ],
)
async def test_graph_dependency_errors_preserve_identity_and_group_structure(
    offline, error
):
    h = offline
    async with httpx.AsyncClient(transport=h.transport) as client:
        with pytest.raises(type(error)) as caught:
            await _invoke(h.db, client, analyzer=AsyncMock(side_effect=error))
    assert caught.value is error
    assert h.db.commits == [1, 2]
    h.result.assert_not_awaited()


async def test_graph_deadline_propagates_without_result_storage(offline):
    cleaned = asyncio.Event()

    async def history(repository, query):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async with httpx.AsyncClient(transport=offline.transport) as client:
        with pytest.raises(InvestigationDeadlineExceeded):
            await _invoke(
                offline.db, client, history_searcher=history, timeout_seconds=0.1
            )
    assert cleaned.is_set()
    offline.result.assert_not_awaited()
    assert offline.db.open_sessions == 0


async def test_github_rate_limit_preserves_deadline_and_stops_before_graph(offline):
    def limited(request):
        if request.url.path == f"/repos/{REPOSITORY}":
            return httpx.Response(200, json={"private": False})
        return httpx.Response(429, headers={"retry-after": "120"})

    history = AsyncMock()
    before = datetime.now(UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
        with pytest.raises(issue_module.GitHubRateLimitError) as caught:
            await _invoke(offline.db, client, history_searcher=history)
    assert caught.value.not_before >= before + timedelta(seconds=120)
    assert offline.db.commits == [1]
    offline.snapshot.assert_not_awaited()
    offline.result.assert_not_awaited()
    history.assert_not_awaited()


@pytest.mark.parametrize("strategy", ["planner", "tool"])
async def test_optional_source_and_follow_up_dependencies_reach_real_graph(
    offline, strategy
):
    offline.identity.side_effect = None
    offline.identity.return_value = (REPOSITORY, 123, INDEX_ID)
    source = AsyncMock(return_value=[_source_hit()])
    if strategy == "planner":
        follow_up = AsyncMock(
            return_value=FollowUpQueryPlan(query=None, reason="Stop planning.")
        )
        options = {"query_planner": follow_up}
    else:
        follow_up = AsyncMock(
            return_value={
                "tool_name": "search_source",
                "tool_call_id": "call-1",
                "query": "cleanup",
                "evidence": [],
                "history_evidence": [],
                "source_evidence": [],
                "skipped_duplicate": True,
                "model_response": None,
            }
        )
        options = {"tool_search_runner": follow_up}
    async with httpx.AsyncClient(transport=offline.transport) as client:
        await _invoke(
            offline.db,
            client,
            source_searcher=source,
            source_index_id=INDEX_ID,
            analyzer=AsyncMock(return_value=_analysis(needs_more=True)),
            max_search_rounds=2,
            **options,
        )
    assert source.await_args.args[0:2] == (REPOSITORY, INDEX_ID)
    source.assert_awaited_once()
    follow_up.assert_awaited_once()
    assert offline.results[0].source_index_id == INDEX_ID
    assert len(offline.results[0].source_evidence) == 1
    assert offline.results[0].next_step == "needs_more_evidence"


@pytest.mark.parametrize("phase", ["github", "history", "analysis", "result_write"])
async def test_external_cancellation_cleans_sessions_and_preserves_signal(
    offline, phase
):
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def block(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    options = {}
    transport = offline.transport
    if phase == "github":
        transport = httpx.MockTransport(block)
    elif phase == "history":
        options["history_searcher"] = block
    elif phase == "analysis":
        options["analyzer"] = block
    else:
        offline.result.side_effect = block
    async with httpx.AsyncClient(transport=transport) as client:
        task = asyncio.create_task(_invoke(offline.db, client, **options))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            assert cleaned.is_set()
            assert offline.db.open_sessions == offline.db.transactions == 0
            assert 3 not in offline.db.commits
            assert not client.is_closed
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _github_transport(request):
    if request.url.path == f"/repos/{REPOSITORY}":
        return httpx.Response(200, json={"private": False})
    assert request.url.path == f"/repos/{REPOSITORY}/issues/123"
    return httpx.Response(200, json=_issue_payload())


async def _rows(factory, task_id):
    async with factory() as session:
        return (
            await session.get(InvestigationTask, task_id),
            await session.get(InvestigationIssueSnapshot, task_id),
            await session.get(InvestigationResultRecord, task_id),
        )


@pytest.fixture
async def english_task(investigation_task):
    factory, task_id, _ = investigation_task
    async with factory() as session, session.begin():
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(response_language="en")
        )
    return investigation_task


@requires_postgres
@pytest.mark.parametrize("outcome", ["ready", "insufficient", "empty"])
async def test_real_runner_completes_only_after_snapshot_and_result_are_committed(
    english_task, outcome
):
    factory, task_id, _ = english_task
    history = AsyncMock(return_value=() if outcome == "empty" else (_history_hit(),))
    analyzer = AsyncMock(return_value=_analysis(needs_more=outcome == "insufficient"))
    observed = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_github_transport)
    ) as client:

        async def business(token):
            await _invoke(
                factory,
                client,
                task_id=task_id,
                claim_token=token,
                history_searcher=history,
                analyzer=analyzer,
            )
            task, snapshot, result = await _rows(factory, task_id)
            assert task.status == "running"
            assert task.claim_token == token
            assert snapshot.title == _issue_payload()["title"]
            assert result.result_payload["issue_title"] == snapshot.title
            observed.append(result.result_payload)

        await runner.execute_investigation_task(factory, task_id, business)
        assert not client.is_closed
    task, _, result = await _rows(factory, task_id)
    assert task.status == "succeeded"
    assert task.claim_token is task.lease_expires_at is None
    assert len(observed) == 1
    assert result.result_payload["next_step"] == (
        "draft_preliminary_report" if outcome == "ready" else "needs_more_evidence"
    )
    if outcome == "empty":
        analyzer.assert_not_awaited()
        assert result.result_payload["analysis_evidence"] == []


@requires_postgres
@pytest.mark.parametrize(
    "case", ["pending", "wrong_token", "expired", "null_lease", "missing"]
)
async def test_claim_identity_reader_uses_database_state_and_time(
    investigation_task, case
):
    factory, task_id, _ = investigation_task
    async with factory() as session:
        async with session.begin():
            token = await claim_investigation_task(session, task_id)
    async with factory() as session:
        async with session.begin():
            assert await get_claimed_investigation_identity(
                session, task_id=task_id, claim_token=token
            ) == (REPOSITORY, 123, None)
    changes = {}
    if case == "pending":
        changes["status"] = "pending"
    elif case == "wrong_token":
        token = "wrong-token"
    elif case == "expired":
        changes["lease_expires_at"] = func.clock_timestamp() - timedelta(seconds=1)
    elif case == "null_lease":
        changes["lease_expires_at"] = None
    else:
        task_id = "missing-task"
    if changes:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    update(InvestigationTask)
                    .where(InvestigationTask.id == task_id)
                    .values(**changes)
                )
    async with factory() as session:
        async with session.begin():
            assert (
                await get_claimed_investigation_identity(
                    session, task_id=task_id, claim_token=token
                )
                is None
            )


@requires_postgres
async def test_real_identity_mismatch_is_permanent_without_external_calls(
    english_task,
):
    factory, task_id, _ = english_task
    requests = []

    def forbidden(request):
        requests.append(request)
        raise AssertionError("Identity mismatch must precede GitHub calls")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:

        async def business(token):
            await _invoke(
                factory, client, task_id=task_id, claim_token=token, issue_number=999
            )

        with pytest.raises(runner.PermanentInvestigationError):
            await runner.execute_investigation_task(factory, task_id, business)
    assert requests == []
    task, snapshot, result = await _rows(factory, task_id)
    assert task.status == "failed"
    assert snapshot is result is None


@requires_postgres
@pytest.mark.parametrize(
    "kind",
    [
        "upstream_timeout",
        "graph_deadline",
        "invalid_citation",
        "result_write",
        "result_commit",
    ],
)
async def test_real_runner_failure_never_marks_success(english_task, monkeypatch, kind):
    factory, task_id, _ = english_task
    real_writer = processor_module.upsert_investigation_result
    commit_target = factory.class_.sync_session_class
    error = RuntimeError("Result persistence failed")
    analyzer = AsyncMock(return_value=_analysis())
    options = {}
    if kind == "upstream_timeout":
        analyzer.side_effect = TimeoutError("Upstream timeout")
    elif kind == "graph_deadline":

        async def blocked_history(repository, query):
            await asyncio.Event().wait()

        options = {"history_searcher": blocked_history, "timeout_seconds": 0.5}
    elif kind == "invalid_citation":
        analyzer.return_value = _analysis(evidence_id="history:item:999")

    async def fail_result(session, **kwargs):
        saved = await real_writer(session, **kwargs)
        assert saved
        if kind == "result_write":
            raise error
        if kind == "result_commit":
            session.info["processor_commit_failure"] = True
        return saved

    def fail_commit(session):
        if session.info.get("processor_commit_failure"):
            raise error

    monkeypatch.setattr(processor_module, "upsert_investigation_result", fail_result)
    event.listen(commit_target, "before_commit", fail_commit)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_github_transport)
        ) as client:

            async def business(token):
                await _invoke(
                    factory,
                    client,
                    task_id=task_id,
                    claim_token=token,
                    analyzer=analyzer,
                    **options,
                )

            error_type = (
                runner.PermanentInvestigationError
                if kind == "invalid_citation"
                else runner.RetryableInvestigationError
            )
            with pytest.raises(error_type):
                await runner.execute_investigation_task(factory, task_id, business)
    finally:
        event.remove(commit_target, "before_commit", fail_commit)
    task, snapshot, result = await _rows(factory, task_id)
    assert task.status == ("failed" if kind == "invalid_citation" else "pending")
    assert task.claim_token is None
    assert snapshot is not None
    assert result is None


@requires_postgres
async def test_replacement_claim_survives_result_write_rejection(english_task):
    factory, task_id, _ = english_task
    new_token = "replacement-owner"

    async def history(repository, query):
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    update(InvestigationTask)
                    .where(InvestigationTask.id == task_id)
                    .values(claim_token=new_token)
                )
        return (_history_hit(),)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_github_transport)
    ) as client:

        async def business(token):
            await _invoke(
                factory,
                client,
                task_id=task_id,
                claim_token=token,
                history_searcher=history,
            )

        with pytest.raises(ExceptionGroup) as caught:
            await runner.execute_investigation_task(factory, task_id, business)
    _, remaining = caught.value.split(runner.LeaseLostError)
    assert remaining is None
    task, snapshot, result = await _rows(factory, task_id)
    assert task.status == "running"
    assert task.claim_token == new_token
    assert snapshot is not None
    assert result is None


@requires_postgres
@pytest.mark.parametrize("cancel_source", ["external", "heartbeat"])
async def test_real_runner_cancellation_cleans_graph_without_completing(
    english_task, monkeypatch, cancel_source
):
    factory, task_id, _ = english_task
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def history(repository, query):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def lost_heartbeat(**kwargs):
        await started.wait()
        raise runner.LeaseLostError("Heartbeat lost ownership")

    if cancel_source == "heartbeat":
        monkeypatch.setattr(runner, "maintain_investigation_lease", lost_heartbeat)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_github_transport)
    ) as client:

        async def business(token):
            await _invoke(
                factory,
                client,
                task_id=task_id,
                claim_token=token,
                history_searcher=history,
                timeout_seconds=30,
            )

        task = asyncio.create_task(
            runner.execute_investigation_task(factory, task_id, business)
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            if cancel_source == "external":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
            else:
                with pytest.raises(ExceptionGroup) as caught:
                    await asyncio.wait_for(task, timeout=5)
                _, remaining = caught.value.split(runner.LeaseLostError)
                assert remaining is None
            assert cleaned.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    stored, snapshot, result = await _rows(factory, task_id)
    assert stored.status == "running"
    assert stored.claim_token is not None
    assert snapshot is not None
    assert result is None
