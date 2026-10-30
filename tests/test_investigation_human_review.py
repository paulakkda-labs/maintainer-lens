"""Offline review contracts, real compiled Graph boundaries and API isolation."""

import asyncio
import importlib.util
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from langgraph.errors import NodeCancelledError
from pydantic import ValidationError

import maintainer_lens.api.routes.investigation_reviews as routes
import maintainer_lens.investigation_runner as runner
from maintainer_lens.agents.investigation_analysis import (
    AnalysisGenerationIncompleteError,
    InvestigationAnalysis,
)
from maintainer_lens.api.dependencies import get_session_factory
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_checkpoint import (
    FrozenInvestigationRun,
    GraphCheckpointConfig,
    execution_fingerprint,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationInput
from maintainer_lens.schemas.investigation_review import (
    ReviewContext,
    ReviewDecision,
    allowed_preparation_profiles,
)
from maintainer_lens.services.investigation_checkpoint_execution import (
    execute_checkpointed_investigation,
)
from maintainer_lens.services.investigation_execution import (
    InvestigationDeadlineExceeded,
)
from maintainer_lens.services.investigation_preparation import (
    KnowledgePreparationDeadlineExceeded,
)
from maintainer_lens.services.investigation_review import (
    ReviewConflictError,
    ReviewNotFoundError,
    apply_approval,
    build_review_context,
    review_trigger,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)
from maintainer_lens.services.repository_index_preparation import (
    KnowledgePreparationBudgetExceeded,
    _check_limit,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _config(**changes):
    return GraphCheckpointConfig(
        response_language="en",
        max_search_rounds=3,
        timeout_seconds=5.0,
        search_strategy="deterministic",
        source_enabled=False,
        history_top_k=5,
        source_top_k=5,
        embedding_provider="test",
        embedding_model="embed",
        embedding_dimensions=3,
        embedding_endpoint_fingerprint="1" * 64,
        llm_model="user-selected-model",
        llm_endpoint_fingerprint="2" * 64,
        llm_max_output_tokens=4096,
        thinking_enabled=False,
        **changes,
    )


def _context(reason="source_files", *, profile="small", observed=None):
    return ReviewContext(
        reason=reason,
        observed=observed,
        knowledge_budget=make_knowledge_preparation_budget(profile),
        base_execution=_config(),
        output_tokens=4096,
    )


def _decision(action="increase_budget", **changes):
    return ReviewDecision(decision_id=uuid4(), action=action, **changes)


@pytest.mark.parametrize(
    "reason,profile,observed,allowed",
    [
        ("source_files", "small", 21, ["standard", "large", "unlimited"]),
        ("source_bytes", "standard", 8388609, ["unlimited"]),
        ("source_files", "small", 501, ["large", "unlimited"]),
        ("source_chunks", "large", 4001, ["unlimited"]),
        ("preparation_timeout_seconds", "standard", None, ["large", "unlimited"]),
        ("source_bytes", "unlimited", None, []),
        ("analysis_generation_limit", "small", None, []),
    ],
)
def test_preparation_choices_raise_the_affected_limit_without_lowering_other_limits(
    reason, profile, observed, allowed
):
    assert (
        allowed_preparation_profiles(
            _context(reason, profile=profile, observed=observed)
        )
        == allowed
    )


@pytest.mark.parametrize("value", [True, 0, -1, "8192", 8192.0, float("inf")])
def test_output_approval_requires_a_finite_strict_positive_integer(value):
    with pytest.raises(ValidationError):
        _decision(max_output_tokens=value)


def test_decision_json_uuid_is_accepted_without_coercing_budget_values():
    decision = ReviewDecision.model_validate(
        {
            "decision_id": str(uuid4()),
            "action": "increase_budget",
            "max_output_tokens": 8192,
        }
    )
    assert decision.max_output_tokens == 8192
    for extra in (
        {"repository": "foreign/repo"},
        {"state": {}},
        {"max_search_rounds": 99},
        {"llm_model": "other"},
    ):
        with pytest.raises(ValidationError):
            ReviewDecision.model_validate({**decision.model_dump(), **extra})


@pytest.mark.parametrize(
    "action,fields",
    [
        ("increase_budget", {}),
        (
            "increase_budget",
            {"max_output_tokens": 8192, "knowledge_budget_profile": "large"},
        ),
        ("reject", {"max_output_tokens": 8192}),
        ("accept_partial", {"knowledge_budget_profile": "unlimited"}),
    ],
)
def test_action_and_budget_selection_are_mutually_consistent(action, fields):
    with pytest.raises(ValidationError):
        _decision(action, **fields)


def test_model_independent_output_increase_preserves_original_execution():
    context = _context("analysis_generation_limit")
    budget, output = apply_approval(context, _decision(max_output_tokens=12345))
    assert output == 12345 and context.base_execution.llm_model == "user-selected-model"
    assert context.base_execution.llm_max_output_tokens == context.output_tokens == 4096
    assert budget == context.knowledge_budget
    for value in (1024, 4096):
        with pytest.raises(ValueError):
            apply_approval(context, _decision(max_output_tokens=value))
    with pytest.raises(ValueError):
        apply_approval(context, _decision(knowledge_budget_profile="unlimited"))
    with pytest.raises(ValueError):
        apply_approval(context, _decision("accept_partial"))


def test_source_budget_error_exposes_only_typed_limit_metadata():
    with pytest.raises(KnowledgePreparationBudgetExceeded) as caught:
        _check_limit("source bytes", 8388609, 8388608)
    assert review_trigger(caught.value) == ("source_bytes", 8388609)
    assert review_trigger(ExceptionGroup("hidden", [caught.value])) == (
        "source_bytes",
        8388609,
    )
    assert (
        review_trigger(
            ExceptionGroup("hidden", [caught.value, runner.LeaseLostError("lost")])
        )
        is None
    )


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            KnowledgePreparationDeadlineExceeded("hidden"),
            ("preparation_timeout_seconds", None),
        ),
        (
            AnalysisGenerationIncompleteError("length"),
            ("analysis_generation_limit", None),
        ),
        (AnalysisGenerationIncompleteError("content_filter"), None),
        (InvestigationDeadlineExceeded("hidden"), None),
        (TimeoutError("hidden"), None),
        (ValueError("hidden"), None),
        (runner.LeaseLostError("hidden"), None),
    ],
)
def test_only_confirmed_soft_budget_failures_open_a_review(error, expected):
    assert review_trigger(error) == expected


class _MemoryStore:
    """SQL substitute; Saver serialization and compiled Graph remain real."""

    def __init__(self):
        self.task_id, self.claim_token = "review-task", "claim"
        request = InvestigationInput(
            repository="review-tests/repo",
            issue_number=123,
            issue_title="Cleanup",
            issue_body=None,
            response_language="en",
        )
        config = _config()
        self.run = FrozenInvestigationRun(
            task_id=self.task_id,
            request=request,
            execution=config,
            fingerprint=execution_fingerprint(request, config),
        )
        self.frames, self.writes = {}, {}

    async def load_run(self):
        return self.run

    async def read_checkpoint(self, namespace, checkpoint_id=None):
        keys = [cid for ns, cid in self.frames if ns == namespace]
        row = self.frames.get((namespace, checkpoint_id or max(keys, default=None)))
        if row is None:
            return None
        return replace(
            row,
            writes=tuple(
                value
                for (ns, cid, _, _), value in self.writes.items()
                if (ns, cid) == (namespace, row.checkpoint_id)
            ),
        )

    async def write_checkpoint(self, row):
        self.frames[(row.namespace, row.checkpoint_id)] = row

    async def write_nodes(self, namespace, checkpoint_id, rows):
        for row in rows:
            key = namespace, checkpoint_id, row.node_task_id, row.write_index
            if row.write_index < 0 or key not in self.writes:
                self.writes[key] = row


def _hit(number):
    return RepositoryHistorySemanticHit(
        repository="review-tests/repo",
        number=number,
        source_type="item",
        comment_id=None,
        title="History",
        content="Release resources.",
        source_url=f"https://github.com/review-tests/repo/issues/{number}",
        source_updated_at=datetime(2026, 10, 29, tzinfo=UTC),
        distance=0.1,
    )


async def _run_graph(store, searcher, analyzer):
    return await execute_checkpointed_investigation(
        store.run, store, history_searcher=searcher, analyzer=analyzer
    )


@pytest.mark.parametrize("with_previous_analysis", [False, True])
async def test_review_reads_only_complete_committed_analysis_and_preserves_search_rounds(
    with_previous_analysis,
):
    store = _MemoryStore()
    searcher = AsyncMock(side_effect=[(_hit(124),), (_hit(125),)])
    complete = InvestigationAnalysis(
        summary="Complete first analysis.",
        findings=[
            {
                "statement": "History discusses cleanup.",
                "evidence_ids": ["history:item:124"],
            }
        ],
        hypotheses=[],
        unanswered_questions=["What happens next?"],
    )
    analyzer = AsyncMock(
        side_effect=[complete, AnalysisGenerationIncompleteError("length")]
        if with_previous_analysis
        else AnalysisGenerationIncompleteError("length")
    )
    with pytest.raises(AnalysisGenerationIncompleteError):
        await _run_graph(store, searcher, analyzer)
    context = await build_review_context(
        store,
        trigger=("analysis_generation_limit", None),
        budget=make_knowledge_preparation_budget("small"),
        base_execution=store.run.execution,
        output_tokens=4096,
    )
    assert context.checkpoint_id and context.run_fingerprint == store.run.fingerprint
    assert context.search_round == (2 if with_previous_analysis else 1)
    if with_previous_analysis:
        assert context.partial_result.search_round == 2
        assert "Human accepted" not in context.partial_result.routing_reason
        assert "human consideration" in context.partial_result.routing_reason
        assert context.partial_result.preliminary_analysis.summary == complete.summary
        assert context.partial_result.preliminary_analysis.findings[0].evidence_ids == [
            "history:item:124"
        ]
        assert (
            context.partial_result.analysis_evidence[0]["evidence_id"]
            == "history:item:124"
        )
    else:
        assert context.partial_result is None
    calls = searcher.await_count
    finish = InvestigationAnalysis(
        summary="Complete resumed analysis.",
        findings=[
            {
                "statement": "History supports cleanup.",
                "evidence_ids": ["history:item:124"],
            }
        ],
        hypotheses=[],
        unanswered_questions=[],
    )
    result = await _run_graph(store, searcher, AsyncMock(return_value=finish))
    assert result.search_round == (2 if with_previous_analysis else 1)
    assert searcher.await_count == calls
    assert store.run.execution.llm_max_output_tokens == 4096


async def test_cancelled_graph_does_not_become_a_review_or_success():
    store = _MemoryStore()
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def analyzer(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    operation = asyncio.create_task(
        _run_graph(store, AsyncMock(return_value=(_hit(124),)), analyzer)
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        operation.cancel("external-human-review-test")
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(operation, 3)
        assert caught.value.args == ("external-human-review-test",)
        assert operation.cancelled() and cleaned.is_set()
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_node_raised_cancellation_preserves_sdk_failure_and_is_not_reviewable():
    store = _MemoryStore()
    cancellation = asyncio.CancelledError("node-raised cancellation")
    analyzer = AsyncMock(side_effect=cancellation)
    with pytest.raises(NodeCancelledError) as caught:
        await _run_graph(store, AsyncMock(return_value=(_hit(124),)), analyzer)
    assert caught.value.node == "analyze_evidence"
    assert caught.value.__cause__ is cancellation
    assert review_trigger(caught.value) is None


async def test_runner_commits_suspension_without_marking_success_failure_or_retry(
    monkeypatch,
):
    import maintainer_lens.db.investigation_review as persistence

    events = []

    @asynccontextmanager
    async def transaction():
        events.append("begin")
        try:
            yield
        except BaseException:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(begin=transaction)

    monkeypatch.setattr(
        runner, "claim_investigation_task", AsyncMock(return_value="claim")
    )
    monkeypatch.setattr(runner, "maintain_investigation_lease", AsyncMock())
    suspend = AsyncMock(side_effect=lambda *args, **kwargs: events.append("suspend"))
    monkeypatch.setattr(persistence, "suspend_task_for_review", suspend)
    terminal = AsyncMock(side_effect=AssertionError("Review must not complete a task"))
    for name in (
        "mark_investigation_succeeded",
        "mark_investigation_failed",
        "release_investigation_task_for_retry",
    ):
        monkeypatch.setattr(runner, name, terminal)
    context = _context()
    await runner.execute_investigation_task(
        factory,
        "task",
        AsyncMock(side_effect=runner.InvestigationReviewRequired(context)),
    )
    assert events[-3:] == ["begin", "suspend", "commit"]
    terminal.assert_not_awaited()


@pytest.mark.parametrize(
    "error,status",
    [
        (ReviewConflictError("stale"), 409),
        (ReviewNotFoundError("missing"), 404),
        (ValueError("fake-secret"), 422),
    ],
)
async def test_review_api_accepts_uuid_and_returns_safe_expected_errors(
    monkeypatch, error, status
):
    app = FastAPI()
    app.include_router(routes.router, prefix="/investigations")
    app.dependency_overrides[get_session_factory] = lambda: object()
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(llm_review_max_output_tokens=None),
    )
    decide = AsyncMock(side_effect=error)
    monkeypatch.setattr(routes, "decide_review", decide)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/investigations/task/reviews/{uuid4()}/decision",
            json={"decision_id": str(uuid4()), "action": "reject"},
        )
    assert response.status_code == status and "fake-secret" not in response.text
    decide.assert_awaited_once()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_review_migration_has_backfill_and_refuses_destructive_downgrade(direction):
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/1bc6f8a0d417_add_investigation_human_review.py"
    )
    spec = importlib.util.spec_from_file_location("review_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = StringIO()
    migration.op = Operations(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
    )
    getattr(migration, direction)()
    sql = output.getvalue()
    assert migration.down_revision == "0ab5e7f9c306"
    if direction == "upgrade":
        assert "SET user_recovery_count = execution_generation" in sql
        assert "UNIQUE (task_id, generation)" in sql and "ON DELETE CASCADE" in sql
    else:
        assert sql.index("RAISE EXCEPTION") < sql.index("DROP TABLE")


def test_deployment_budget_configuration_does_not_restrict_model_names():
    from maintainer_lens.core.config import Settings
    from maintainer_lens.services.investigation_configuration import (
        make_worker_execution_config,
    )

    kwargs = dict(
        _env_file=None,
        database_url="postgresql+asyncpg://test:test@localhost/test",
        rabbitmq_url="amqp://test:test@localhost/",
        llm_base_url="https://user-provider.example/v1",
        llm_model_name="my-custom-model",
        llm_api_key=None,
        embedding_api_key=None,
        github_token=None,
        embedding_base_url="https://embed.example/v1",
        embedding_model="my-embedding",
        embedding_dimensions=3,
        llm_max_output_tokens=6000,
        llm_review_max_output_tokens=12000,
    )
    settings = Settings(**kwargs)
    profile = make_worker_execution_config(settings, source_enabled=True)
    assert (
        profile.llm_model == "my-custom-model" and profile.llm_max_output_tokens == 6000
    )
    with pytest.raises(ValidationError, match="ceiling"):
        Settings(**{**kwargs, "llm_review_max_output_tokens": 5000})
    assert (
        Settings(
            **{**kwargs, "llm_review_max_output_tokens": None}
        ).llm_review_max_output_tokens
        is None
    )


async def test_invalid_analysis_reference_is_not_eligible_for_human_budget_review():
    from maintainer_lens.agents.investigation.state import (
        InvalidInvestigationResultError,
    )

    store = _MemoryStore()
    invalid = InvestigationAnalysis(
        summary="Invalid citation.",
        findings=[{"statement": "Unsupported.", "evidence_ids": ["history:item:999"]}],
        hypotheses=[],
        unanswered_questions=[],
    )
    with pytest.raises(InvalidInvestigationResultError) as caught:
        await _run_graph(
            store, AsyncMock(return_value=(_hit(124),)), AsyncMock(return_value=invalid)
        )
    assert review_trigger(caught.value) is None


@pytest.mark.parametrize("action", ["increase_budget", "reject"])
async def test_decision_queries_never_autoflush_an_incomplete_decision_pair(
    monkeypatch, action
):
    import maintainer_lens.services.investigation_review as service
    from maintainer_lens.db.models.investigation import (
        InvestigationOutboxEvent,
        InvestigationTask,
    )
    from maintainer_lens.db.models.investigation_checkpoint import InvestigationGraphRun
    from maintainer_lens.db.models.investigation_review import InvestigationReview

    now = datetime(2026, 10, 29, tzinfo=UTC)
    context = _context(observed=21)
    task = InvestigationTask(
        id="review-autoflush-task",
        response_language="en",
        repository="review-tests/repo",
        issue_number=123,
        status="waiting_review",
        execution_generation=0,
        budget_resume_count=0,
        user_recovery_count=0,
        auto_prepare_knowledge=False,
        source_index_id=None,
        prepared_source_index_id=None,
        claim_token=None,
        lease_expires_at=None,
        failure_retryable=False,
        knowledge_budget=context.knowledge_budget.model_dump(mode="json"),
    )
    row = InvestigationReview(
        id=uuid4(),
        task_id=task.id,
        generation=0,
        created_at=now,
        context_payload=context.model_dump(mode="json"),
        decision_payload=None,
        decided_at=None,
        target_generation=None,
    )
    added = []

    def check_database_constraints():
        # Model the real constraint at every query/flush boundary, not at each
        # Python assignment. SQLAlchemy may autoflush before a SELECT.
        assert (row.decision_payload is None) == (row.decided_at is None)
        if row.target_generation is not None:
            assert row.decision_payload is not None
            assert row.target_generation > row.generation

    class Session:
        @asynccontextmanager
        async def begin(self):
            yield
            check_database_constraints()

        async def get(self, model, identity):
            check_database_constraints()
            if model is InvestigationReview:
                assert identity == row.id
                return row
            assert model is InvestigationGraphRun and identity == task.id
            return None

        async def scalar(self, statement):
            check_database_constraints()
            assert "clock_timestamp" in str(statement)
            return now

        def add(self, value):
            added.append(value)

        async def flush(self):
            check_database_constraints()

    @asynccontextmanager
    async def factory():
        yield Session()

    monkeypatch.setattr(service, "lock_task", AsyncMock(return_value=task))
    decision = _decision(
        action,
        **(
            {"knowledge_budget_profile": "standard"}
            if action == "increase_budget"
            else {}
        ),
    )
    result = await service.decide_review(
        factory,
        task_id=task.id,
        review_id=row.id,
        decision=decision,
        configuration_factory=lambda source: context.base_execution,
    )
    assert result.decision == decision and result.decided_at == now
    assert task.status == ("pending" if action == "increase_budget" else "failed")
    count = sum(isinstance(value, InvestigationOutboxEvent) for value in added)
    assert count == (1 if action == "increase_budget" else 0)
    repeat = await service.decide_review(
        factory,
        task_id=task.id,
        review_id=row.id,
        decision=decision,
        configuration_factory=lambda source: context.base_execution,
    )
    assert repeat == result
    assert sum(isinstance(value, InvestigationOutboxEvent) for value in added) == count
