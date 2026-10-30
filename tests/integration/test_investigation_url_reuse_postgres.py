"""Opt-in real PostgreSQL URL arbitration, recovery generations and progress.

No provider/broker calls. Apply the Alembic migration manually first. Each test
owns a unique repository and removes only its own lookups/tasks/index on exit.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update

from maintainer_lens.api.routes.investigations import router
from maintainer_lens.clients.github import GitHubIssue
from maintainer_lens.db.investigation_progress import (
    append_task_event,
    read_task_events,
)
from maintainer_lens.db.investigation_tasks import claim_investigation_task
from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_checkpoint import InvestigationGraphRun
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.db.models.investigation_reuse import (
    InvestigationTaskEvent,
    InvestigationUrlLookup,
)
from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.investigation_runner import (
    LeaseLostError,
    RetryableInvestigationError,
    execute_investigation_task,
)
from maintainer_lens.messaging.handler import process_investigation_request
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.schemas.investigation_checkpoint import (
    GraphCheckpointConfig,
    execution_fingerprint,
)
from maintainer_lens.schemas.investigation_contracts import (
    InvestigationInput,
    InvestigationResult,
)
from maintainer_lens.services.investigation_freshness import IssueObservation
from maintainer_lens.services.investigation_progress import (
    InvestigationProgressReporter,
)
from maintainer_lens.services.investigation_url_reuse import resolve_investigation_url

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration tests are disabled",
    ),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def scope(session_factory):
    repository = f"url-reuse-tests/repo-{uuid4().hex}"
    config = GraphCheckpointConfig(
        response_language="zh",
        max_search_rounds=3,
        timeout_seconds=120.0,
        search_strategy="tool",
        source_enabled=True,
        tool_timeout_seconds=60.0,
        history_top_k=5,
        source_top_k=5,
        embedding_provider="test",
        embedding_model="embed",
        embedding_dimensions=3,
        embedding_endpoint_fingerprint="1" * 64,
        llm_model="analysis",
        llm_endpoint_fingerprint="2" * 64,
        llm_max_output_tokens=4096,
        thinking_enabled=False,
    )
    data = SimpleNamespace(
        factory=session_factory,
        repository=repository,
        index_id=uuid4(),
        now=datetime.now(UTC),
        config=config,
    )
    try:
        yield data
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(InvestigationUrlLookup).where(
                    InvestigationUrlLookup.repository == repository
                )
            )
            await session.execute(
                delete(InvestigationTask).where(
                    InvestigationTask.repository == repository
                )
            )
            await session.execute(
                delete(RepositoryIndex).where(RepositoryIndex.id == data.index_id)
            )


def _observation(scope, *, commit=None, state="open"):
    issue = GitHubIssue(
        number=123,
        title="Cleanup",
        body="Release resources.",
        state=state,
        html_url=f"https://github.com/{scope.repository}/issues/123",
        created_at=scope.now,
        updated_at=scope.now,
    )
    return IssueObservation(issue, commit or "a" * 40, scope.now)


async def _resolve(scope, observer=None, **kwargs):
    kwargs.setdefault("response_language", "zh")
    return await resolve_investigation_url(
        scope.factory,
        repository=scope.repository,
        issue_number=123,
        observer=observer or AsyncMock(return_value=_observation(scope)),
        configuration_factory=lambda: scope.config,
        **kwargs,
    )


async def _finish(
    scope, task_id, *, status="succeeded", retryable=False, response_language="zh"
):
    request = InvestigationInput(
        repository=scope.repository,
        issue_number=123,
        issue_title="Cleanup",
        issue_body="Release resources.",
        response_language=response_language,
        source_index_id=scope.index_id,
    )
    url = f"https://github.com/{scope.repository}/issues/124"
    result = InvestigationResult(
        **request.model_dump(),
        next_step="draft_preliminary_report",
        preliminary_analysis={
            "summary": "Saved report.",
            "findings": [
                {
                    "statement": "Cleanup is discussed.",
                    "evidence_ids": ["history:item:124"],
                }
            ],
            "hypotheses": [],
            "unanswered_questions": [],
        },
        source_evidence=[],
        history_evidence=[
            {
                "repository": scope.repository,
                "number": 124,
                "source_type": "item",
                "comment_id": None,
                "title": "History",
                "content": "Release resources.",
                "source_url": url,
                "source_updated_at": scope.now.isoformat(),
                "distance": 0.1,
            }
        ],
        analysis_evidence=[
            {
                "evidence_id": "history:item:124",
                "category": "history",
                "source_url": url,
                "content": "History\nRelease resources.",
            }
        ],
        search_round=1,
        routing_reason="Cited findings without declared gaps.",
    )
    async with scope.factory() as session, session.begin():
        if await session.get(RepositoryIndex, scope.index_id) is None:
            session.add(
                RepositoryIndex(
                    id=scope.index_id,
                    repository=scope.repository,
                    commit_sha="a" * 40,
                    tree_sha="b" * 40,
                    embedding_provider="test",
                    embedding_model="embed",
                    embedding_dimensions=3,
                    chunker_version="investigation-auto-v1",
                    status="ready",
                )
            )
        task = await session.get(InvestigationTask, task_id)
        task.status, task.failure_retryable, task.prepared_source_index_id = (
            status,
            retryable,
            scope.index_id,
        )
        session.add(
            InvestigationIssueSnapshot(
                task_id=task_id,
                title=request.issue_title,
                body=request.issue_body,
                state="open",
                source_url=f"https://github.com/{scope.repository}/issues/123",
                source_created_at=scope.now,
                source_updated_at=scope.now,
                fetched_at=scope.now,
            )
        )
        session.add(
            InvestigationGraphRun(
                task_id=task_id,
                storage_version=1,
                request_payload=request.model_dump(mode="json"),
                execution_payload=scope.config.model_copy(
                    update={"response_language": response_language}
                ).model_dump(mode="json"),
                fingerprint=execution_fingerprint(
                    request,
                    scope.config.model_copy(
                        update={"response_language": response_language}
                    ),
                ),
            )
        )
        if status == "succeeded":
            session.add(
                InvestigationResultRecord(
                    task_id=task_id,
                    schema_version=1,
                    result_payload=result.model_dump(mode="json"),
                )
            )


async def test_postgres_concurrent_first_submissions_create_one_task_and_outbox(scope):
    observer = AsyncMock(
        side_effect=AssertionError("No metadata requests on a new URL")
    )
    results = await asyncio.gather(*[_resolve(scope, observer) for _ in range(8)])
    assert len({result.task_id for result in results}) == 1
    assert sum(result.decision == "created" for result in results) == 1
    observer.assert_not_awaited()
    async with scope.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationTask)
                .where(InvestigationTask.repository == scope.repository)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationOutboxEvent)
                .where(InvestigationOutboxEvent.task_id == results[0].task_id)
            )
            == 1
        )


async def test_postgres_metadata_check_reuses_report_without_holding_task_or_lookup_locks(
    scope,
):
    task = await _resolve(scope)
    await _finish(scope, task.task_id)

    async def observe(*args):
        async with scope.factory() as session, session.begin():
            assert (
                await session.scalar(
                    select(InvestigationTask.id)
                    .where(InvestigationTask.id == task.task_id)
                    .with_for_update(nowait=True)
                )
                == task.task_id
            )
            assert await session.scalar(
                select(InvestigationUrlLookup.intent_key)
                .where(InvestigationUrlLookup.repository == scope.repository)
                .with_for_update(nowait=True)
            )
        return _observation(scope)

    result = await _resolve(scope, observe)
    assert result.task_id == task.task_id and result.decision == "reused"
    assert result.report_markdown and result.freshness.status == "current"


async def test_postgres_changed_source_retains_report_through_new_task_failure_and_get(
    scope,
):
    original = await _resolve(scope)
    await _finish(scope, original.task_id)
    fresh = await _resolve(
        scope,
        AsyncMock(return_value=_observation(scope, commit="e" * 40, state="closed")),
    )
    assert (
        fresh.task_id != original.task_id and fresh.previous_task_id == original.task_id
    )
    assert (
        fresh.report_task_id == original.task_id and "已关闭" in fresh.report_markdown
    )
    assert fresh.freshness.status == "stale"
    async with scope.factory() as session, session.begin():
        task = await session.get(InvestigationTask, fresh.task_id)
        task.status, task.failure_retryable = "failed", False
    again = await _resolve(
        scope, AsyncMock(side_effect=httpx.ConnectError("fake-secret"))
    )
    assert again.task_id == fresh.task_id and again.report_task_id == original.task_id
    assert (
        again.freshness.status == "unknown"
        and "fake-secret" not in again.model_dump_json()
    )
    app = FastAPI()
    app.state.session_factory = scope.factory
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        detail = await client.get(f"/investigations/{fresh.task_id}")
    assert detail.status_code == 200
    assert detail.json()["result"] is None and detail.json()["report_markdown"] is None
    assert detail.json()["previous_report_task_id"] == original.task_id
    assert detail.json()["previous_report_markdown"]


async def test_postgres_concurrent_recovery_advances_once_and_rejects_late_generation(
    scope,
):
    original = await _resolve(scope)
    await _finish(scope, original.task_id, status="failed", retryable=True)

    async def observe(*args):
        await asyncio.sleep(0.02)
        return _observation(scope)

    results = await asyncio.gather(*[_resolve(scope, observe) for _ in range(8)])
    assert all(result.task_id == original.task_id for result in results)
    assert sum(result.decision == "resumed" for result in results) == 1
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, original.task_id)
        rows = (
            await session.scalars(
                select(InvestigationOutboxEvent).where(
                    InvestigationOutboxEvent.task_id == task.id
                )
            )
        ).all()
        assert task.status == "pending" and task.execution_generation == 1
        assert sorted(row.generation for row in rows) == [0, 1]
    processor = AsyncMock()
    await process_investigation_request(
        InvestigationRequestedMessage(
            task_id=original.task_id,
            repository=scope.repository,
            issue_number=123,
            auto_prepare_knowledge=True,
        ),
        scope.factory,
        processor,
    )
    processor.assert_not_awaited()
    async with scope.factory() as session:
        assert (
            await session.get(InvestigationTask, original.task_id)
        ).status == "pending"
    async with scope.factory() as session, session.begin():
        assert await claim_investigation_task(session, original.task_id) is None
        assert await claim_investigation_task(
            session, original.task_id, expected_generation=1
        )


async def test_postgres_recovery_limit_and_permanent_error_do_not_dispatch_again(scope):
    original = await _resolve(scope)
    await _finish(scope, original.task_id, status="failed", retryable=True)
    async with scope.factory() as session, session.begin():
        task = await session.get(InvestigationTask, original.task_id)
        task.execution_generation = 3
        task.user_recovery_count = 3
    blocked = await _resolve(scope)
    assert blocked.decision == "blocked" and blocked.generation == 3
    async with scope.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationOutboxEvent)
                .where(InvestigationOutboxEvent.task_id == original.task_id)
            )
            == 1
        )


async def test_postgres_expired_check_cannot_replace_newer_successful_check(scope):
    original = await _resolve(scope)
    await _finish(scope, original.task_id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def old(*args):
        entered.set()
        await release.wait()
        return _observation(scope, commit="e" * 40)

    operation = asyncio.create_task(_resolve(scope, old))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with scope.factory() as session, session.begin():
            await session.execute(
                update(InvestigationUrlLookup)
                .where(InvestigationUrlLookup.repository == scope.repository)
                .values(check_expires_at=func.clock_timestamp() - timedelta(seconds=1))
            )
        assert (await _resolve(scope)).decision == "reused"
        release.set()
        late = await operation
        assert late.task_id == original.task_id and late.freshness.status == "current"
        async with scope.factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(InvestigationTask)
                    .where(InvestigationTask.repository == scope.repository)
                )
                == 1
            )
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_postgres_cancelled_metadata_check_releases_token_and_preserves_report(
    scope,
):
    original = await _resolve(scope)
    await _finish(scope, original.task_id)
    entered = asyncio.Event()

    async def block(*args):
        entered.set()
        await asyncio.Event().wait()

    operation = asyncio.create_task(_resolve(scope, block))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        async with scope.factory() as session:
            lookup = await session.scalar(
                select(InvestigationUrlLookup).where(
                    InvestigationUrlLookup.repository == scope.repository
                )
            )
            assert lookup.check_token is None
            assert await session.get(InvestigationResultRecord, original.task_id)
        assert (await _resolve(scope)).decision == "reused"
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_postgres_runner_persists_safe_error_phase_and_terminal_status_together(
    scope,
):
    original = await _resolve(scope)

    async def processor(token):
        reporter = InvestigationProgressReporter(scope.factory, original.task_id, token)
        await reporter.phase("analyzing", 2)
        raise TimeoutError("fake-secret-provider-response")

    with pytest.raises(RetryableInvestigationError):
        await execute_investigation_task(
            scope.factory,
            original.task_id,
            processor,
            final_attempt=True,
            event_recorder=append_task_event,
        )
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, original.task_id)
        assert (
            task.status == "failed"
            and task.failure_retryable
            and task.claim_token is None
        )
        page = await read_task_events(session, task.id, limit=2)
        assert page.next_after is not None and len(page.events) == 2
        rest = await read_task_events(session, task.id, after=page.next_after)
        last = rest.events[-1]
        assert (
            last.kind == "failed"
            and last.phase == "analyzing"
            and last.search_round == 2
        )
        assert (
            last.error.code == "upstream_timeout"
            and "fake-secret" not in rest.model_dump_json()
        )
    # There was no frozen Run yet, so resubmission can retry preparation safely.
    resumed = await _resolve(scope)
    assert resumed.task_id == original.task_id and resumed.generation == 1


@pytest.mark.parametrize("failed", [False, True])
async def test_postgres_terminal_event_rolls_back_when_status_fence_rejects(
    scope, monkeypatch, failed
):
    import maintainer_lens.investigation_runner as runner

    original = await _resolve(scope)
    monkeypatch.setattr(
        runner,
        "mark_investigation_failed" if failed else "mark_investigation_succeeded",
        AsyncMock(return_value=False),
    )

    async def processor(token):
        reporter = InvestigationProgressReporter(scope.factory, original.task_id, token)
        await reporter.phase("analyzing", 1)
        if failed:
            raise TimeoutError("fake-secret")

    with pytest.raises(LeaseLostError):
        await execute_investigation_task(
            scope.factory,
            original.task_id,
            processor,
            final_attempt=True,
            event_recorder=append_task_event,
        )
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, original.task_id)
        assert task.status == "running" and not task.failure_retryable
        page = await read_task_events(session, task.id)
        assert [event.kind for event in page.events] == [
            "queued",
            "started",
            "progress",
        ]


async def test_postgres_progress_is_claim_fenced_and_rollback_does_not_publish_events(
    scope,
):
    original = await _resolve(scope)
    async with scope.factory() as session, session.begin():
        token = await claim_investigation_task(session, original.task_id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with scope.factory() as session, session.begin():
            await append_task_event(
                session,
                task_id=original.task_id,
                claim_token=token,
                kind="progress",
                phase="analyzing",
                search_round=1,
            )
            raise RuntimeError("rollback")
    async with scope.factory() as session, session.begin():
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == original.task_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    with pytest.raises(LeaseLostError):
        await InvestigationProgressReporter(
            scope.factory, original.task_id, token
        ).phase("analyzing", 1)
    async with scope.factory() as session:
        rows = (
            await session.scalars(
                select(InvestigationTaskEvent).where(
                    InvestigationTaskEvent.task_id == original.task_id
                )
            )
        ).all()
        assert [row.kind for row in rows] == ["queued"]


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_postgres_lookup_rejects_foreign_intent_before_external_calls(
    scope, language
):
    import maintainer_lens.services.investigation_url_reuse as module

    original = await _resolve(scope, response_language=language)
    async with scope.factory() as session, session.begin():
        original_task = await session.get(InvestigationTask, original.task_id)
        other_id = str(uuid4())
        session.add(
            InvestigationTask(
                id=other_id,
                response_language=language,
                repository=scope.repository,
                issue_number=124,
                auto_prepare_knowledge=True,
                knowledge_budget=original_task.knowledge_budget,
                status="pending",
            )
        )
        await session.flush()
        await session.execute(
            update(InvestigationUrlLookup)
            .where(InvestigationUrlLookup.repository == scope.repository)
            .values(task_id=other_id)
        )
    observer = AsyncMock()
    with pytest.raises(ValueError, match="different investigation intent"):
        await module.resolve_investigation_url(
            scope.factory,
            repository=scope.repository,
            issue_number=123,
            response_language=language,
            observer=observer,
        )
    observer.assert_not_awaited()


async def test_postgres_new_task_outbox_and_lookup_roll_back_together(
    scope, monkeypatch
):
    import maintainer_lens.services.investigation_url_reuse as module

    original = module.add_investigation_submission

    async def fail_after_staging(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("rollback staged submission")

    monkeypatch.setattr(module, "add_investigation_submission", fail_after_staging)
    with pytest.raises(RuntimeError, match="rollback staged"):
        await _resolve(scope)
    async with scope.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationTask)
                .where(InvestigationTask.repository == scope.repository)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationUrlLookup)
                .where(InvestigationUrlLookup.repository == scope.repository)
            )
            == 0
        )


async def test_postgres_get_does_not_claim_an_expired_check_is_still_running(scope):
    import json

    from maintainer_lens.schemas.investigation_reuse import InvestigationFreshness

    original = await _resolve(scope)
    await _finish(scope, original.task_id)
    async with scope.factory() as session, session.begin():
        task = await session.get(InvestigationTask, original.task_id)
        task.freshness_payload = json.loads(
            InvestigationFreshness(status="checking").model_dump_json()
        )
        await session.execute(
            update(InvestigationUrlLookup)
            .where(InvestigationUrlLookup.repository == scope.repository)
            .values(
                check_token="expired",
                check_expires_at=func.clock_timestamp() - timedelta(seconds=1),
            )
        )
    app = FastAPI()
    app.state.session_factory = scope.factory
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        detail = await client.get(f"/investigations/{original.task_id}")
    assert detail.status_code == 200
    assert detail.json()["freshness"]["status"] == "unknown"
    assert "无法确认" in detail.json()["report_markdown"]
    async with scope.factory() as session:
        # GET remains read-only; it does not take over or restart the check.
        assert (
            await session.get(InvestigationTask, original.task_id)
        ).freshness_payload["status"] == "checking"


async def test_postgres_languages_isolate_tasks_and_reuse_same_language_reports(scope):
    english = await _resolve(scope, response_language="en")
    chinese = await _resolve(scope, response_language="zh")
    assert english.task_id != chinese.task_id
    await _finish(scope, english.task_id, response_language="en")
    await _finish(scope, chinese.task_id, response_language="zh")
    reused_en, reused_zh = await asyncio.gather(
        _resolve(scope, response_language="en"), _resolve(scope, response_language="zh")
    )
    assert reused_en.decision == reused_zh.decision == "reused"
    assert reused_en.report_task_id == english.task_id
    assert reused_zh.report_task_id == chinese.task_id
    assert reused_en.response_language == "en" and reused_zh.response_language == "zh"


async def test_postgres_english_recovery_preserves_language_in_the_next_delivery(scope):
    created = await _resolve(scope, response_language="en")
    await _finish(
        scope, created.task_id, status="failed", retryable=True, response_language="en"
    )
    resumed = await _resolve(scope, response_language="en")
    assert resumed.decision == "resumed" and resumed.task_id == created.task_id
    async with scope.factory() as session:
        outbox = await session.scalar(
            select(InvestigationOutboxEvent).where(
                InvestigationOutboxEvent.task_id == created.task_id,
                InvestigationOutboxEvent.generation == 1,
            )
        )
        assert outbox.payload["response_language"] == "en"
