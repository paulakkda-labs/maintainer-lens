"""Offline URL decisions, freshness and race handling; SQL is tested separately."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

import maintainer_lens.services.investigation_url_reuse as reuse
from maintainer_lens.clients.github import GitHubIssue
from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_checkpoint import InvestigationGraphRun
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.db.models.investigation_reuse import InvestigationTaskEvent
from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_checkpoint import (
    GraphCheckpointConfig,
    execution_fingerprint,
)
from maintainer_lens.schemas.investigation_contracts import (
    InvestigationInput,
    InvestigationResult,
)
from maintainer_lens.services.investigation_freshness import IssueObservation

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 29, tzinfo=UTC)
REPO = "url-tests/repository"
INDEX = UUID("12345678-1234-4234-8234-123456789abc")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _config(**changes):
    return GraphCheckpointConfig.model_validate(
        {
            "response_language": "zh",
            "max_search_rounds": 3,
            "timeout_seconds": 120.0,
            "search_strategy": "tool",
            "source_enabled": True,
            "history_top_k": 5,
            "source_top_k": 5,
            "embedding_provider": "test",
            "embedding_model": "embed",
            "embedding_dimensions": 3,
            "embedding_endpoint_fingerprint": "1" * 64,
            "llm_model": "analysis",
            "llm_endpoint_fingerprint": "2" * 64,
            "llm_max_output_tokens": 4096,
            "thinking_enabled": False,
            "tool_timeout_seconds": 60.0,
            **changes,
        }
    )


def _request():
    return InvestigationInput(
        repository=REPO,
        issue_number=123,
        issue_title="Cleanup",
        issue_body="Release resources.",
        response_language="zh",
        source_index_id=INDEX,
    )


def _result():
    source_id = f"source:{INDEX}:{'c' * 64}"
    url = f"https://github.com/{REPO}/blob/{'a' * 40}/client.py#L1-L2"
    return InvestigationResult(
        **_request().model_dump(),
        next_step="draft_preliminary_report",
        preliminary_analysis={
            "summary": "Saved report.",
            "findings": [
                {
                    "statement": "Cleanup releases resources.",
                    "evidence_ids": [source_id],
                }
            ],
            "hypotheses": [],
            "unanswered_questions": [],
        },
        history_evidence=[],
        source_evidence=[
            {
                "index_id": str(INDEX),
                "chunk_key": "c" * 64,
                "repository": REPO,
                "commit_sha": "a" * 40,
                "blob_sha": "b" * 40,
                "file_path": "client.py",
                "language": "python",
                "kind": "code",
                "symbol": "cleanup",
                "start_line": 1,
                "end_line": 2,
                "content": "def cleanup():\n    release()",
                "source_url": url,
                "rrf_score": 0.03,
                "vector_rank": 1,
                "keyword_rank": None,
                "cosine_distance": 0.1,
                "matched_in": None,
            }
        ],
        analysis_evidence=[
            {
                "evidence_id": source_id,
                "category": "source",
                "source_url": url,
                "content": "client.py:1-2\ndef cleanup():\n    release()",
            }
        ],
        search_round=1,
        routing_reason="Cited findings without declared gaps.",
    )


def _observation(**changes):
    payload = {
        "number": 123,
        "title": "Cleanup",
        "body": "Release resources.",
        "state": "open",
        "html_url": f"https://github.com/{REPO}/issues/123",
        "created_at": NOW,
        "updated_at": NOW,
        **changes,
    }
    commit = payload.pop("commit_sha", "a" * 40)
    return IssueObservation(GitHubIssue.model_validate(payload), commit, NOW)


class _Memory:
    """Replace SQL I/O, while executing the complete URL decision service."""

    def __init__(self):
        self.records, self.lookups, self.events, self.outbox = {}, {}, [], []
        self.lock, self.open_transactions = asyncio.Lock(), 0
        self.now = NOW

    def add(self, row):
        if isinstance(row, InvestigationTask):
            self.records[(InvestigationTask, row.id)] = row
        elif isinstance(row, InvestigationOutboxEvent):
            self.outbox.append(row)
        elif isinstance(row, InvestigationTaskEvent):
            self.events.append(row)
        else:
            raise AssertionError(type(row))

    async def flush(self):
        pass

    async def get(self, model, identity):
        return self.records.get((model, identity))

    async def scalar(self, statement):
        assert "clock_timestamp" in str(statement)
        return self.now

    @asynccontextmanager
    async def locked(self, repository, issue_number, budget):
        key = (repository.casefold(), issue_number, budget["profile"])
        async with self.lock:
            self.open_transactions += 1
            lookup = self.lookups.setdefault(
                key,
                SimpleNamespace(
                    task_id=None,
                    report_task_id=None,
                    check_token=None,
                    check_expires_at=None,
                ),
            )
            try:
                yield (
                    self,
                    lookup,
                    self.records.get((InvestigationTask, lookup.task_id)),
                )
            finally:
                self.open_transactions -= 1

    def seed(self, *, status="succeeded", retryable=False, generation=0):
        task = InvestigationTask(
            id="original",
            repository=REPO,
            issue_number=123,
            auto_prepare_knowledge=True,
            source_index_id=None,
            prepared_source_index_id=INDEX,
            status=status,
            execution_generation=generation,
            user_recovery_count=generation,
            budget_resume_count=0,
            failure_retryable=retryable,
            knowledge_budget=make_knowledge_preparation_budget().model_dump(
                mode="json"
            ),
        )
        self.add(task)
        self.lookups[(REPO, 123, "standard")] = SimpleNamespace(
            task_id=task.id,
            report_task_id=None,
            check_token=None,
            check_expires_at=None,
        )
        config, request = _config(), _request()
        self.records[(InvestigationGraphRun, task.id)] = InvestigationGraphRun(
            task_id=task.id,
            storage_version=1,
            request_payload=request.model_dump(mode="json"),
            execution_payload=config.model_dump(mode="json"),
            fingerprint=execution_fingerprint(request, config),
        )
        self.records[(RepositoryIndex, INDEX)] = RepositoryIndex(
            id=INDEX,
            repository=REPO,
            commit_sha="a" * 40,
            tree_sha="d" * 40,
            embedding_provider="test",
            embedding_model="embed",
            embedding_dimensions=3,
            chunker_version="investigation-auto-v1",
            status="ready",
        )
        self.records[(InvestigationIssueSnapshot, task.id)] = (
            InvestigationIssueSnapshot(
                task_id=task.id,
                title="Cleanup",
                body="Release resources.",
                state="open",
                source_updated_at=NOW,
                source_created_at=NOW,
                source_url=f"https://github.com/{REPO}/issues/123",
                fetched_at=NOW,
            )
        )
        self.records[(InvestigationResultRecord, task.id)] = InvestigationResultRecord(
            task_id=task.id,
            schema_version=1,
            result_payload=_result().model_dump(mode="json"),
        )
        return task


@pytest.fixture
def memory(monkeypatch):
    memory = _Memory()
    monkeypatch.setattr(reuse, "InvestigationUrlStore", lambda factory: memory)
    return memory


async def _resolve(memory, observer=None, **kwargs):
    return await reuse.resolve_investigation_url(
        object(),
        repository=REPO,
        issue_number=123,
        observer=observer or AsyncMock(return_value=_observation()),
        configuration_factory=lambda: _config(),
        **kwargs,
    )


async def test_first_url_creates_one_task_and_outbox_without_external_calls(memory):
    observer = AsyncMock(
        side_effect=AssertionError("First submission must remain asynchronous")
    )
    result = await _resolve(memory, observer)
    assert result.decision == "created" and result.status == "pending"
    assert len(memory.outbox) == len(memory.events) == 1
    assert memory.outbox[0].task_id == result.task_id
    observer.assert_not_awaited()


@pytest.mark.parametrize("status", ["pending", "running"])
async def test_active_task_is_reused_without_duplicate_dispatch(memory, status):
    memory.seed(status=status)
    observer = AsyncMock()
    result = await _resolve(memory, observer)
    assert result.decision == "active" and result.task_id == "original"
    assert not memory.outbox
    observer.assert_not_awaited()


async def test_successful_report_reuse_checks_metadata_outside_transactions(memory):
    memory.seed()

    async def observe(*args):
        assert memory.open_transactions == 0
        return _observation()

    result = await _resolve(memory, observe)
    assert result.decision == "reused" and result.report_task_id == "original"
    assert r"Saved report\." in result.report_markdown
    assert "打开中" in result.report_markdown
    assert result.freshness.status == "current" and result.freshness.checked_at == NOW
    assert not memory.outbox


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Changed"},
        {"body": "Changed"},
        {"state": "closed"},
        {"updated_at": NOW + timedelta(seconds=1)},
        {"commit_sha": "e" * 40},
    ],
)
async def test_changed_inputs_preserve_old_report_and_queue_new_task(memory, change):
    original = memory.seed()
    run = memory.records[(InvestigationGraphRun, original.id)]
    result = await _resolve(memory, AsyncMock(return_value=_observation(**change)))
    assert result.task_id != original.id and result.previous_task_id == original.id
    assert result.decision == "created" and result.report_task_id == original.id
    assert result.freshness.status == "stale" and len(memory.outbox) == 1
    assert (
        original.status == "succeeded"
        and run.request_payload == _request().model_dump(mode="json")
    )
    if change.get("state") == "closed":
        assert "已关闭" in result.report_markdown


async def test_model_change_starts_new_task_without_rewriting_frozen_run(memory):
    memory.seed()
    result = await reuse.resolve_investigation_url(
        object(),
        repository=REPO,
        issue_number=123,
        observer=AsyncMock(return_value=_observation()),
        configuration_factory=lambda: _config(llm_model="new-model"),
    )
    assert result.decision == "created" and result.task_id != "original"
    assert result.freshness.reason_code == "execution_changed"


@pytest.mark.parametrize("previous_version", ["investigation-v1", "investigation-v2"])
async def test_analysis_version_change_keeps_old_report_and_queues_a_new_task(
    memory, previous_version
):
    memory.seed()
    record = memory.records[(InvestigationGraphRun, "original")]
    original_request = _request()
    previous_execution = _config(graph_version=previous_version)
    record.execution_payload = previous_execution.model_dump(mode="json")
    record.fingerprint = execution_fingerprint(original_request, previous_execution)
    previous_fingerprint = record.fingerprint

    result = await _resolve(memory)

    assert _config().graph_version == "investigation-v3"
    assert result.decision == "created" and result.task_id != "original"
    assert result.previous_task_id == result.report_task_id == "original"
    assert result.freshness.reason_code == "execution_changed"
    assert result.report_markdown and len(memory.outbox) == 1
    assert record.execution_payload["graph_version"] == previous_version
    assert record.fingerprint == previous_fingerprint
    assert record.request_payload == original_request.model_dump(mode="json")


async def test_analysis_version_change_queues_new_work_after_permanent_output_failure(
    memory,
):
    task = memory.seed(status="failed", retryable=False)
    del memory.records[(InvestigationResultRecord, task.id)]
    record = memory.records[(InvestigationGraphRun, task.id)]
    previous_execution = _config(graph_version="investigation-v2")
    record.execution_payload = previous_execution.model_dump(mode="json")
    record.fingerprint = execution_fingerprint(_request(), previous_execution)
    previous_fingerprint = record.fingerprint

    result = await _resolve(memory)

    assert result.decision == "created" and result.task_id != task.id
    assert result.previous_task_id == task.id
    assert result.report_task_id is None and result.report_markdown is None
    assert result.freshness.reason_code == "execution_changed"
    assert len(memory.outbox) == 1 and memory.outbox[0].generation == 0
    assert task.status == "failed" and task.user_recovery_count == 0
    assert record.fingerprint == previous_fingerprint
    assert record.execution_payload == previous_execution.model_dump(mode="json")


async def test_metadata_error_returns_old_report_with_unknown_freshness_and_safe_reason(
    memory,
):
    memory.seed()
    secret = "fake-secret-do-not-persist"
    failure = httpx.ConnectError(secret)
    result = await _resolve(memory, AsyncMock(side_effect=failure))
    assert result.decision == "unverified" and result.report_markdown
    assert (
        result.freshness.status == "unknown" and secret not in result.model_dump_json()
    )
    assert not memory.outbox


@pytest.mark.parametrize(
    "retryable,generation,decision",
    [
        (True, 0, "resumed"),
        (True, 2, "resumed"),
        (True, 3, "blocked"),
        (False, 0, "blocked"),
    ],
)
async def test_controlled_recovery_uses_same_task_and_new_delivery_generation(
    memory, retryable, generation, decision
):
    task = memory.seed(status="failed", retryable=retryable, generation=generation)
    result = await _resolve(memory)
    assert result.task_id == task.id and result.decision == decision
    if decision == "resumed":
        assert result.generation == generation + 1 and task.status == "pending"
        message = InvestigationRequestedMessage.model_validate(memory.outbox[0].payload)
        assert message.generation == generation + 1 == memory.outbox[0].generation
        again = await _resolve(memory)
        assert again.decision == "active" and len(memory.outbox) == 1
    else:
        assert task.status == "failed" and not memory.outbox


async def test_concurrent_initial_submissions_dispatch_once(memory):
    results = await asyncio.gather(*[_resolve(memory) for _ in range(8)])
    assert len({result.task_id for result in results}) == 1
    assert sum(result.decision == "created" for result in results) == 1
    assert len(memory.outbox) == 1


async def test_concurrent_user_recovery_dispatches_once(memory):
    memory.seed(status="failed", retryable=True)
    results = await asyncio.gather(*[_resolve(memory) for _ in range(8)])
    assert len({result.task_id for result in results}) == 1
    assert len(memory.outbox) == 1 and memory.outbox[0].generation == 1


async def test_inflight_check_is_shared_and_external_cancellation_releases_it(memory):
    memory.seed()
    entered = asyncio.Event()

    async def observe(*args):
        entered.set()
        await asyncio.Event().wait()

    operation = asyncio.create_task(_resolve(memory, observe))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        follower = await _resolve(
            memory, AsyncMock(side_effect=AssertionError("Duplicate check"))
        )
        assert follower.decision == "checking" and follower.report_markdown
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        lookup = memory.lookups[(REPO, 123, "standard")]
        assert lookup.check_token is None
        assert (await _resolve(memory)).decision == "reused"
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_expired_check_cannot_overwrite_a_replacement_observation(memory):
    memory.seed()
    entered, release = asyncio.Event(), asyncio.Event()

    async def old(*args):
        entered.set()
        await release.wait()
        return _observation(commit_sha="e" * 40)

    operation = asyncio.create_task(_resolve(memory, old))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        memory.now += timedelta(seconds=31)
        assert (await _resolve(memory)).decision == "reused"
        release.set()
        late = await operation
        assert late.task_id == "original" and late.freshness.status == "current"
        assert not memory.outbox
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_expired_check_without_replacement_is_unknown_and_can_be_retried(memory):
    memory.seed()

    async def slow(*args):
        memory.now += timedelta(seconds=31)
        return _observation()

    result = await _resolve(memory, slow)
    assert result.decision == "unverified" and result.freshness.status == "unknown"
    assert memory.lookups[(REPO, 123, "standard")].check_token is None
    assert (await _resolve(memory)).decision == "reused"


async def test_historical_report_remains_reachable_after_updated_task_fails(memory):
    memory.seed()
    updated = await _resolve(
        memory, AsyncMock(return_value=_observation(commit_sha="e" * 40))
    )
    task = memory.records[(InvestigationTask, updated.task_id)]
    task.status, task.failure_retryable = "failed", False
    result = await _resolve(
        memory, AsyncMock(side_effect=httpx.ConnectError("unavailable"))
    )
    assert result.task_id == updated.task_id and result.report_task_id == "original"
    assert result.report_markdown and result.freshness.status == "unknown"


async def test_corrupt_frozen_identity_is_blocked_without_discarding_progress(memory):
    memory.seed(status="failed", retryable=True)
    row = memory.records[(InvestigationGraphRun, "original")]
    row.fingerprint = "0" * 64
    result = await _resolve(memory)
    assert (
        result.decision == "blocked"
        and result.freshness.reason_code == "invalid_saved_execution"
    )
    assert row.fingerprint == "0" * 64 and not memory.outbox


@pytest.mark.parametrize(
    "status,decision", [("succeeded", "unverified"), ("failed", "blocked")]
)
async def test_missing_frozen_issue_snapshot_cannot_claim_report_is_current(
    memory, status, decision
):
    task = memory.seed(status=status, retryable=True)
    del memory.records[(InvestigationIssueSnapshot, task.id)]
    result = await _resolve(memory)
    assert result.task_id == task.id and result.decision == decision
    assert result.freshness.status == "unknown"
    assert result.freshness.reason_code == "saved_snapshot_missing"
    assert not memory.outbox and task.execution_generation == 0


@pytest.mark.parametrize("profile", ["small", "large", "unlimited"])
async def test_different_preparation_intent_does_not_reuse_an_incompatible_task(
    memory, profile
):
    memory.seed()
    result = await _resolve(memory, knowledge_budget_profile=profile)
    assert result.decision == "created" and result.task_id != "original"
    assert memory.outbox[0].task_id == result.task_id


async def test_waiting_review_url_does_not_probe_approve_or_dispatch(memory):
    task = memory.seed(status="waiting_review")
    observer = AsyncMock(
        side_effect=AssertionError("Waiting must not approve or probe")
    )
    result = await _resolve(memory, observer)
    assert result.task_id == task.id and result.decision == "awaiting_review"
    assert result.status == "waiting_review" and not memory.outbox
    observer.assert_not_awaited()


async def test_delivery_generation_does_not_consume_user_recovery_allowance(memory):
    task = memory.seed(status="failed", retryable=True, generation=5)
    task.user_recovery_count, task.budget_resume_count = 2, 3
    result = await _resolve(memory)
    assert result.decision == "resumed" and result.generation == 6
    assert task.user_recovery_count == 3 and task.budget_resume_count == 3
