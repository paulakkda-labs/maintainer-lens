"""Opt-in PostgreSQL acceptance across recovery, review and report readback.

Use the real API routes, Processor, Runner and compiled checkpointed Graph.
GitHub, knowledge preparation, retrieval and analysis are simulated. This is
not a RabbitMQ/default-Worker assembly or paid-provider test. Apply Alembic head
manually; cleanup deletes only this test's uniquely named repository records.
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

import maintainer_lens.api.routes.investigation_reviews as review_routes
import maintainer_lens.api.routes.investigations as investigation_routes
from maintainer_lens.agents.investigation_analysis import (
    AnalysisGenerationIncompleteError,
    InvestigationAnalysis,
)
from maintainer_lens.clients.github import GitHubIssue
from maintainer_lens.db.investigation_checkpoints import InvestigationCheckpointStore
from maintainer_lens.db.investigation_progress import append_task_event
from maintainer_lens.db.investigation_review import load_approved_review
from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_checkpoint import InvestigationGraphRun
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.db.models.investigation_reuse import InvestigationUrlLookup
from maintainer_lens.db.models.investigation_review import InvestigationReview
from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.investigation_runner import (
    InvestigationReviewRequired,
    LeaseLostError,
    RetryableInvestigationError,
)
from maintainer_lens.messaging.handler import (
    RetryableMessageError,
    process_investigation_request,
)
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_checkpoint import GraphCheckpointConfig
from maintainer_lens.schemas.investigation_review import ReviewContext
from maintainer_lens.services.investigation_freshness import IssueObservation
from maintainer_lens.services.investigation_processor import process_graph_investigation
from maintainer_lens.services.investigation_progress import (
    InvestigationProgressReporter,
)
from maintainer_lens.services.investigation_review import (
    apply_approval,
    build_review_context,
    review_trigger,
)
from maintainer_lens.services.investigation_source_index import (
    get_investigation_source_index,
)
from maintainer_lens.services.investigation_url_reuse import resolve_investigation_url
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)

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


@pytest.fixture(params=["en", "zh"])
async def workflow(session_factory, monkeypatch, request):
    repository = f"workflow-acceptance/repo-{uuid4().hex}"
    now = datetime.now(UTC)
    index_id = uuid4()
    config = GraphCheckpointConfig(
        response_language=request.param,
        max_search_rounds=3,
        timeout_seconds=10.0,
        search_strategy="deterministic",
        source_enabled=True,
        history_top_k=5,
        source_top_k=5,
        embedding_provider="test",
        embedding_model="embed",
        embedding_dimensions=3,
        embedding_endpoint_fingerprint="1" * 64,
        llm_model="configured-test-model",
        llm_endpoint_fingerprint="2" * 64,
        llm_max_output_tokens=4096,
        thinking_enabled=False,
    )
    issue = GitHubIssue(
        number=123,
        title="Cleanup",
        body="Release resources.",
        state="open",
        html_url=f"https://github.com/{repository}/issues/123",
        created_at=now,
        updated_at=now,
    )
    data = SimpleNamespace(
        factory=session_factory,
        repository=repository,
        index_id=index_id,
        config=config,
        commit="a" * 40,
        output_allowances=[],
        stores=[],
    )

    async def observe(repo, number):
        assert (repo, number) == (repository, 123)
        return IssueObservation(issue, data.commit, datetime.now(UTC))

    data.observer = AsyncMock(side_effect=observe)

    async def resolve(factory, **kwargs):
        return await resolve_investigation_url(
            factory,
            **kwargs,
            observer=data.observer,
            configuration_factory=lambda: config,
        )

    monkeypatch.setattr(investigation_routes, "resolve_investigation_url", resolve)
    monkeypatch.setattr(
        review_routes,
        "get_settings",
        lambda: SimpleNamespace(llm_review_max_output_tokens=None),
    )
    # Deliberately use deterministic searches in this service-composition suite.
    # Default Worker/tool-selection assembly is covered by the existing MQ suite.
    monkeypatch.setattr(
        review_routes, "make_worker_execution_config", lambda *args, **kwargs: config
    )

    def github(request):
        if request.url == f"https://api.github.com/repos/{repository}":
            return httpx.Response(200, json={"private": False})
        assert request.url == f"https://api.github.com/repos/{repository}/issues/123"
        return httpx.Response(200, json=issue.model_dump(mode="json"))

    data.github_calls = []

    def transport(request):
        data.github_calls.append(str(request.url))
        return github(request)

    async def validate_source(repo, source_id):
        async with session_factory() as session, session.begin():
            await get_investigation_source_index(
                session,
                repository=repo,
                source_index_id=source_id,
                embedding_provider_name=config.embedding_provider,
                embedding_model=config.embedding_model,
                embedding_dimensions=config.embedding_dimensions,
            )

    data.validate_source = validate_source
    data.preparer = AsyncMock(return_value=index_id)
    data.source = AsyncMock(return_value=())
    data.history = AsyncMock(
        side_effect=[
            (
                RepositoryHistorySemanticHit(
                    repository=repository,
                    number=number,
                    source_type="item",
                    comment_id=None,
                    title="Cleanup history",
                    content="Release resources explicitly.",
                    source_url=f"https://github.com/{repository}/issues/{number}",
                    source_updated_at=now,
                    distance=0.1,
                ),
            )
            for number in (124, 125)
        ]
    )
    app = FastAPI()
    app.state.session_factory = session_factory
    app.include_router(investigation_routes.router)
    try:
        async with session_factory() as session, session.begin():
            session.add(
                RepositoryIndex(
                    id=index_id,
                    repository=repository,
                    commit_sha=data.commit,
                    tree_sha="b" * 40,
                    embedding_provider=config.embedding_provider,
                    embedding_model=config.embedding_model,
                    embedding_dimensions=config.embedding_dimensions,
                    chunker_version="investigation-auto-v1",
                    status="ready",
                )
            )
        async with (
            httpx.AsyncClient(
                transport=httpx.MockTransport(transport)
            ) as github_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as api,
        ):
            data.github, data.api = github_client, api
            yield data
        assert github_client.is_closed and api.is_closed
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
                delete(RepositoryIndex).where(RepositoryIndex.id == index_id)
            )


def _analysis(*, complete=False):
    return InvestigationAnalysis(
        summary="Complete cited analysis.",
        findings=[
            {"statement": "Cleanup is discussed.", "evidence_ids": ["history:item:124"]}
        ],
        hypotheses=[],
        unanswered_questions=[] if complete else ["Which cleanup path is affected?"],
    )


async def _submit(scope):
    response = await scope.api.post(
        "/investigations/from-url",
        json={
            "issue_url": f"https://github.com/{scope.repository}/issues/123",
            "knowledge_budget_profile": "small",
            "response_language": scope.config.response_language,
        },
    )
    assert response.status_code in (200, 202), response.text
    body = response.json()
    assert body["response_language"] == scope.config.response_language
    return body


async def _detail(scope, task_id):
    response = await scope.api.get(f"/investigations/{task_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["response_language"] == scope.config.response_language
    if body["result"] is not None:
        assert body["result"]["response_language"] == scope.config.response_language
    return body


async def _reviews(scope, task_id):
    response = await scope.api.get(f"/investigations/{task_id}/reviews")
    assert response.status_code == 200, response.text
    return response.json()


async def _decide(scope, task_id, review_id, decision):
    return await scope.api.post(
        f"/investigations/{task_id}/reviews/{review_id}/decision", json=decision
    )


async def _deliver(scope, task_id, generation, processor, *, final_attempt=False):
    await process_investigation_request(
        InvestigationRequestedMessage(
            task_id=task_id,
            repository=scope.repository,
            issue_number=123,
            auto_prepare_knowledge=True,
            generation=generation,
            response_language=scope.config.response_language,
        ),
        scope.factory,
        processor,
        final_attempt=final_attempt,
        event_recorder=append_task_event,
    )


async def _process(scope, task_id, token, analyzer):
    """Compose real services without starting the default Worker or a broker."""
    store = InvestigationCheckpointStore(
        scope.factory, task_id=task_id, claim_token=token
    )
    scope.stores.append(store)
    budget, approval = await load_approved_review(
        scope.factory, task_id=task_id, claim_token=token
    )
    output_tokens = scope.config.llm_max_output_tokens
    if approval is not None:
        budget, output_tokens = apply_approval(*approval)
    scope.output_allowances.append(output_tokens)
    try:
        await process_graph_investigation(
            http_client=scope.github,
            session_factory=scope.factory,
            task_id=task_id,
            claim_token=token,
            repository=scope.repository,
            issue_number=123,
            response_language=scope.config.response_language,
            history_searcher=scope.history,
            analyzer=analyzer,
            timeout_seconds=scope.config.timeout_seconds,
            source_searcher=scope.source,
            source_index_validator=scope.validate_source,
            max_search_rounds=scope.config.max_search_rounds,
            auto_prepare_knowledge=True,
            knowledge_preparer=scope.preparer,
            checkpoint_store=store,
            checkpoint_config=scope.config,
            progress=InvestigationProgressReporter(scope.factory, task_id, token),
            review_approval=approval,
        )
    except Exception as error:
        trigger = review_trigger(error)
        if trigger is None:
            raise
        context = await build_review_context(
            store,
            trigger=trigger,
            budget=budget,
            base_execution=scope.config,
            output_tokens=output_tokens,
        )
        raise InvestigationReviewRequired(context) from error


async def _pause_with_previous_analysis(scope):
    submission = await _submit(scope)
    assert submission["decision"] == "created"
    task_id = submission["task_id"]
    analyzer = AsyncMock(
        side_effect=[_analysis(), AnalysisGenerationIncompleteError("length")]
    )

    async def process(token):
        await _process(scope, task_id, token, analyzer)

    await _deliver(scope, task_id, 0, process)
    detail = await _detail(scope, task_id)
    assert detail["status"] == "waiting_review" and detail["result"] is None
    assert detail["last_error"]["code"] == "analysis_generation_limit"
    reviews = await _reviews(scope, task_id)
    assert len(reviews) == 1 and reviews[0]["can_accept_partial"]
    assert reviews[0]["context"]["search_round"] == 2
    assert analyzer.await_count == scope.history.await_count == 2
    scope.preparer.assert_awaited_once()
    return task_id, reviews[0]


async def test_approved_output_survives_user_recovery_without_repeating_retrieval(
    workflow,
):
    scope = workflow
    task_id, review = await _pause_with_previous_analysis(scope)
    decision = {
        "decision_id": str(uuid4()),
        "action": "increase_budget",
        "max_output_tokens": 8192,
    }
    approved = await _decide(scope, task_id, review["review_id"], decision)
    assert approved.status_code == 200 and approved.json()["target_generation"] == 1
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    failure = httpx.HTTPStatusError(
        "private-provider-detail",
        request=request,
        response=httpx.Response(503, request=request),
    )
    analyzer = AsyncMock(side_effect=failure)

    async def fail(token):
        await _process(scope, task_id, token, analyzer)

    with pytest.raises(RetryableInvestigationError):
        await _deliver(scope, task_id, 1, fail, final_attempt=True)
    failed = await _detail(scope, task_id)
    assert failed["status"] == "failed" and failed["generation"] == 1
    assert failed["last_error"]["retryable"] is True
    assert "private-provider-detail" not in str(failed)
    assert len(await _reviews(scope, task_id)) == 1
    resumed = await _submit(scope)
    assert (resumed["task_id"], resumed["decision"], resumed["generation"]) == (
        task_id,
        "resumed",
        2,
    )
    no_work = AsyncMock(side_effect=AssertionError("Old deliveries must not execute"))
    await _deliver(scope, task_id, 0, no_work)
    await _deliver(scope, task_id, 1, no_work)
    no_work.assert_not_awaited()
    # An uncertain HTTP approval response may be replayed after user recovery.
    replay = await _decide(scope, task_id, review["review_id"], decision)
    assert replay.status_code == 200 and replay.json()["target_generation"] == 1
    assert replay.json()["budget_resumes_used"] == 1
    assert replay.json()["user_recoveries_used"] == 1
    complete = AsyncMock(return_value=_analysis(complete=True))

    async def finish(token):
        await _process(scope, task_id, token, complete)

    await _deliver(scope, task_id, 2, finish)
    final = await _detail(scope, task_id)
    assert final["status"] == "succeeded" and final["generation"] == 2
    assert final["result"]["search_round"] == 2
    assert final["result"]["next_step"] == "draft_preliminary_report"
    assert len(final["result"]["history_evidence"]) == 2
    assert len(final["result"]["analysis_evidence"]) == 2
    assert scope.output_allowances == [4096, 8192, 8192]
    assert scope.history.await_count == scope.source.await_count == 2
    assert len(scope.github_calls) == 2
    scope.preparer.assert_awaited_once()
    complete.assert_awaited_once()
    cached = await _submit(scope)
    assert cached["decision"] == "reused" and cached["report_task_id"] == task_id
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, task_id)
        run = await session.get(InvestigationGraphRun, task_id)
        assert task.user_recovery_count == task.budget_resume_count == 1
        assert task.knowledge_budget == make_knowledge_preparation_budget(
            "small"
        ).model_dump(mode="json")
        assert run.execution_payload["llm_max_output_tokens"] == 4096
        assert (
            task.response_language
            == run.request_payload["response_language"]
            == run.execution_payload["response_language"]
            == scope.config.response_language
        )
        assert run.fingerprint == review["context"]["run_fingerprint"]
        deliveries = list(
            await session.scalars(
                select(InvestigationOutboxEvent)
                .where(InvestigationOutboxEvent.task_id == task_id)
                .order_by(InvestigationOutboxEvent.generation)
            )
        )
        assert [delivery.generation for delivery in deliveries] == [0, 1, 2]
        assert all(
            InvestigationRequestedMessage.model_validate(
                delivery.payload
            ).response_language
            == scope.config.response_language
            for delivery in deliveries
        )
    events = (await scope.api.get(f"/investigations/{task_id}/events")).json()["events"]
    assert any(
        event["kind"] == "failed" and event["generation"] == 1 for event in events
    )
    assert events[-1]["kind"] == "completed" and events[-1]["generation"] == 2


async def test_accepted_analysis_survives_cancel_after_result_commit_without_early_report(
    workflow,
):
    scope = workflow
    task_id, review = await _pause_with_previous_analysis(scope)
    decision = {"decision_id": str(uuid4()), "action": "accept_partial"}
    approved = await _decide(scope, task_id, review["review_id"], decision)
    assert approved.status_code == 200 and approved.json()["target_generation"] == 1
    no_analysis = AsyncMock(
        side_effect=AssertionError("No model call after acceptance")
    )
    committed, cleaned, hold = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def interrupt_after_commit(token):
        try:
            await _process(scope, task_id, token, no_analysis)
            committed.set()
            await hold.wait()
        finally:
            cleaned.set()

    operation = asyncio.create_task(_deliver(scope, task_id, 1, interrupt_after_commit))
    try:
        async with asyncio.timeout(10):
            await committed.wait()
        operation.cancel("acceptance-crash-window")
        with pytest.raises(asyncio.CancelledError, match="acceptance-crash-window"):
            await operation
    finally:
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
    assert cleaned.is_set()
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert task.status == "running" and task.execution_generation == 1
        record = await session.get(InvestigationResultRecord, task_id)
        assert record is not None
        payload = record.result_payload
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationReview)
                .where(InvestigationReview.task_id == task_id)
            )
            == 1
        )
    detail = await _detail(scope, task_id)
    assert detail["result"] is detail["report_markdown"] is None
    active = await _submit(scope)
    assert active["decision"] == "active" and active["report_markdown"] is None
    scope.observer.assert_not_awaited()
    # Simulate lease expiry after a crashed process; do not wait fifteen minutes.
    async with scope.factory() as session, session.begin():
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
    with pytest.raises(LeaseLostError):
        await scope.stores[-1].load_run()

    async def redeliver(token):
        await _process(scope, task_id, token, no_analysis)

    await _deliver(scope, task_id, 1, redeliver)
    final = await _detail(scope, task_id)
    assert final["status"] == "succeeded" and final["generation"] == 1
    assert final["result"] == payload
    assert final["result"]["next_step"] == "needs_more_evidence"
    assert final["result"]["search_round"] == 2
    assert "Human accepted" in final["result"]["routing_reason"]
    # Later retrieval succeeded, but only the first analysis completed.
    assert len(final["result"]["history_evidence"]) == 2
    assert [item["evidence_id"] for item in final["result"]["analysis_evidence"]] == [
        "history:item:124"
    ]
    no_analysis.assert_not_awaited()
    assert scope.history.await_count == scope.source.await_count == 2
    assert len(scope.github_calls) == 2
    scope.preparer.assert_awaited_once()
    await _deliver(scope, task_id, 1, no_analysis)
    no_analysis.assert_not_awaited()
    replay = await _decide(scope, task_id, review["review_id"], decision)
    assert replay.status_code == 200
    assert (
        replay.json()["budget_resumes_used"]
        == replay.json()["user_recoveries_used"]
        == 0
    )
    assert (await _submit(scope))["decision"] == "reused"
    async with scope.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationResultRecord)
                .where(InvestigationResultRecord.task_id == task_id)
            )
            == 1
        )
        deliveries = list(
            await session.scalars(
                select(InvestigationOutboxEvent)
                .where(InvestigationOutboxEvent.task_id == task_id)
                .order_by(InvestigationOutboxEvent.generation)
            )
        )
        assert [delivery.generation for delivery in deliveries] == [0, 1]
        assert all(
            InvestigationRequestedMessage.model_validate(
                delivery.payload
            ).response_language
            == scope.config.response_language
            for delivery in deliveries
        )


async def test_rejecting_new_investigation_preserves_old_report_and_blocks_resubmission(
    workflow,
):
    scope = workflow
    first = await _submit(scope)
    old_id = first["task_id"]

    async def finish(token):
        await _process(
            scope, old_id, token, AsyncMock(return_value=_analysis(complete=True))
        )

    await _deliver(scope, old_id, 0, finish)
    assert (await _submit(scope))["decision"] == "reused"
    scope.commit = "c" * 40
    newer = await _submit(scope)
    assert newer["decision"] == "created" and newer["previous_task_id"] == old_id
    assert newer["report_task_id"] == old_id and newer["freshness"]["status"] == "stale"
    new_id = newer["task_id"]
    assert new_id != old_id

    async def preparation_limit(token):
        raise InvestigationReviewRequired(
            ReviewContext(
                reason="source_files",
                observed=21,
                knowledge_budget=make_knowledge_preparation_budget("small"),
                base_execution=scope.config,
                output_tokens=4096,
            )
        )

    await _deliver(scope, new_id, 0, preparation_limit)
    waiting = await _submit(scope)
    assert (
        waiting["decision"] == "awaiting_review" and waiting["report_task_id"] == old_id
    )
    review = (await _reviews(scope, new_id))[0]
    assert review["can_accept_partial"] is False
    assert review["context"]["partial_result"] is None
    decision = {"decision_id": str(uuid4()), "action": "reject"}
    rejected = await _decide(scope, new_id, review["review_id"], decision)
    assert rejected.status_code == 200 and rejected.json()["target_generation"] is None
    # Same rejection is safe to repeat; changing it into approval is a conflict.
    assert (
        await _decide(scope, new_id, review["review_id"], decision)
    ).status_code == 200
    conflicting = await _decide(
        scope,
        new_id,
        review["review_id"],
        {
            "decision_id": str(uuid4()),
            "action": "increase_budget",
            "knowledge_budget_profile": "standard",
        },
    )
    assert conflicting.status_code == 409
    for _ in range(2):
        blocked = await _submit(scope)
        assert blocked["decision"] == "blocked" and blocked["task_id"] == new_id
        assert blocked["report_task_id"] == old_id and blocked["report_markdown"]
        assert blocked["freshness"]["status"] == "stale"
    detail = await _detail(scope, new_id)
    assert detail["status"] == "failed"
    assert detail["last_error"]["code"] == "review_rejected"
    assert detail["last_error"]["retryable"] is False
    assert detail["result"] is detail["report_markdown"] is None
    assert detail["previous_report_task_id"] == old_id
    assert detail["previous_report_markdown"]
    no_work = AsyncMock(side_effect=AssertionError("Rejected task must not execute"))
    # A failed current generation is routed by the handler, not ACKed as success.
    with pytest.raises(RetryableMessageError) as caught:
        await _deliver(scope, new_id, 0, no_work)
    assert caught.value.status == "failed"
    no_work.assert_not_awaited()
    async with scope.factory() as session:
        task = await session.get(InvestigationTask, new_id)
        assert (
            task.execution_generation
            == task.budget_resume_count
            == task.user_recovery_count
            == 0
        )
        assert await session.get(InvestigationGraphRun, new_id) is None
        assert await session.get(InvestigationResultRecord, new_id) is None
        generations = await session.scalars(
            select(InvestigationOutboxEvent.generation).where(
                InvestigationOutboxEvent.task_id == new_id
            )
        )
        assert list(generations) == [0]
