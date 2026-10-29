"""Offline progress safety, API contracts, migration SQL and Graph observation."""

import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from pydantic import ValidationError

import maintainer_lens.api.routes.investigations as routes
import maintainer_lens.investigation_runner as runner
import maintainer_lens.services.investigation_freshness as freshness
from maintainer_lens.agents.investigation_graph import build_investigation_graph
from maintainer_lens.api.dependencies import get_session_factory
from maintainer_lens.db.models.investigation import InvestigationOutboxEvent
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.schemas.investigation_reuse import (
    InvestigationFreshness,
    ResolveInvestigationResponse,
)
from maintainer_lens.services.investigation_freshness import render_observed_status
from maintainer_lens.services.investigation_progress import describe_failure

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("generation", [True, -1, "1", 1.0])
def test_message_generation_is_strict(generation):
    with pytest.raises(ValidationError):
        InvestigationRequestedMessage(
            task_id="task", repository="org/repo", issue_number=1, generation=generation
        )


def test_zero_generation_is_legacy_wire_compatible_and_recovery_is_explicit():
    request = InvestigationRequestedMessage(
        task_id="task", repository="org/repo", issue_number=1
    )
    assert "generation" not in json.loads(request.model_dump_json())
    recovered = request.model_copy(update={"generation": 1})
    assert json.loads(recovered.model_dump_json())["generation"] == 1
    assert (
        InvestigationRequestedMessage.model_validate_json(
            recovered.model_dump_json()
        ).generation
        == 1
    )


@pytest.mark.parametrize(
    "error,retryable,code",
    [
        (ValueError("fake-secret"), False, "invalid_data"),
        (TimeoutError("fake-secret"), True, "upstream_timeout"),
        (RuntimeError("fake-secret"), True, "processing_failed"),
        (
            ExceptionGroup("fake-secret", [ValueError("fake-secret")]),
            False,
            "invalid_data",
        ),
    ],
)
def test_failure_descriptions_do_not_store_raw_exception_text(error, retryable, code):
    result = describe_failure(error, retryable=retryable)
    assert result.retryable == retryable and result.code == code
    assert "fake-secret" not in result.model_dump_json()


def test_wrapped_http_error_preserves_safe_status_without_url_or_credentials():
    request = httpx.Request(
        "GET", "https://api.github.com/repos/example/project?token=fake-secret"
    )
    response = httpx.Response(404, request=request)
    original = httpx.HTTPStatusError("fake-secret", request=request, response=response)
    wrapper = runner.PermanentInvestigationError("fake-secret")
    wrapper.__cause__ = original
    result = describe_failure(wrapper, retryable=False)
    assert result.code == "github_http_404" and "404" in result.message
    assert "fake-secret" not in result.model_dump_json()


@pytest.mark.parametrize("state,label", [("closed", "已关闭"), ("open", "打开中")])
def test_github_status_has_display_priority_without_rewriting_analysis(state, label):
    original = "# 原报告\n\n仍需核查技术修复。"
    report = render_observed_status(
        original, "zh", state=state, checked_at=datetime(2026, 10, 29, tzinfo=UTC)
    )
    assert report.index(label) < report.index("原报告")
    assert report.endswith(original) and "证据" in report


@pytest.mark.parametrize(
    "decision,task_status,http_status",
    [
        ("created", "pending", 202),
        ("active", "running", 202),
        ("resumed", "pending", 202),
        ("checking", "succeeded", 202),
        ("reused", "succeeded", 200),
        ("unverified", "succeeded", 200),
        ("blocked", "failed", 200),
        ("awaiting_review", "waiting_review", 202),
    ],
)
async def test_url_route_returns_selected_task_and_resolution_decision(
    monkeypatch, decision, task_status, http_status
):
    app = FastAPI()
    app.include_router(routes.router)
    factory = object()
    app.dependency_overrides[get_session_factory] = lambda: factory
    resolver = AsyncMock(
        return_value=ResolveInvestigationResponse(
            task_id="existing",
            status=task_status,
            decision=decision,
            generation=1,
            freshness=InvestigationFreshness(),
        )
    )
    monkeypatch.setattr(routes, "resolve_investigation_url", resolver)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/investigations/from-url",
            json={"issue_url": "https://github.com/org/repo/issues/123"},
        )
        invalid = await client.post(
            "/investigations/from-url",
            json={
                "issue_url": "https://github.com/org/repo/issues/123",
                "generation": 5,
            },
        )
    assert response.status_code == http_status and invalid.status_code == 422
    assert (
        response.json()["task_id"] == "existing"
        and response.json()["decision"] == decision
    )
    resolver.assert_awaited_once_with(
        factory,
        repository="org/repo",
        issue_number=123,
        knowledge_budget_profile="standard",
        response_language="en",
    )


async def test_real_graph_reports_node_boundaries_and_propagates_observer_errors():
    observed = []

    async def observer(name, state):
        observed.append(name)

    graph = build_investigation_graph(
        AsyncMock(return_value=()), node_observer=observer
    )
    await graph.ainvoke(
        {
            "repository": "org/repo",
            "issue_number": 123,
            "issue_title": "Cleanup",
            "issue_body": None,
        }
    )
    assert observed == ["prepare_query", "search_history"]
    failure = runner.LeaseLostError("lost event ownership")
    graph = build_investigation_graph(
        AsyncMock(), node_observer=AsyncMock(side_effect=failure)
    )
    with pytest.raises(runner.LeaseLostError) as caught:
        await graph.ainvoke(
            {
                "repository": "org/repo",
                "issue_number": 123,
                "issue_title": "Cleanup",
                "issue_body": None,
            }
        )
    assert caught.value is failure


@pytest.mark.parametrize("permanent", [False, True])
async def test_runner_records_classified_error_before_status_commit(
    monkeypatch, permanent
):
    events = []

    class Session:
        @asynccontextmanager
        async def begin(self):
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
        yield Session()

    monkeypatch.setattr(
        runner, "claim_investigation_task", AsyncMock(return_value="claim")
    )
    monkeypatch.setattr(runner, "maintain_investigation_lease", AsyncMock())

    async def record(session, **kwargs):
        events.append(kwargs["kind"])
        if kwargs.get("error") is not None:
            assert "fake-secret" not in kwargs["error"].model_dump_json()

    async def transition(**kwargs):
        events.append("status")
        return True

    monkeypatch.setattr(runner, "mark_investigation_failed", transition)
    monkeypatch.setattr(runner, "release_investigation_task_for_retry", transition)
    error = ValueError("fake-secret") if permanent else TimeoutError("fake-secret")
    with pytest.raises(
        runner.PermanentInvestigationError
        if permanent
        else runner.RetryableInvestigationError
    ):
        await runner.execute_investigation_task(
            factory, "task", AsyncMock(side_effect=error), event_recorder=record
        )
    kind = "failed" if permanent else "retry_scheduled"
    assert events.index(kind) < events.index("status") < len(events) - 1
    assert events[-1] == "commit"


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_url_reuse_migration_renders_offline_with_safe_generation_boundary(direction):
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0ab5e7f9c306_add_investigation_url_reuse.py"
    )
    spec = importlib.util.spec_from_file_location("url_reuse_migration", path)
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
    assert migration.down_revision == "f9a4d6e8b205"
    if direction == "upgrade":
        assert (
            "CREATE TABLE investigation_url_lookups" in sql
            and "CREATE TABLE investigation_task_events" in sql
        )
        assert "UNIQUE (task_id, event_type, generation)" in sql
        assert "ON DELETE SET NULL" in sql and "ON DELETE CASCADE" in sql
    else:
        assert sql.index("RAISE EXCEPTION") < sql.index("DROP TABLE")
    constraint = next(
        item
        for item in InvestigationOutboxEvent.__table__.constraints
        if item.name == "uq_investigation_outbox_task_event"
    )
    assert list(constraint.columns.keys()) == ["task_id", "event_type", "generation"]


@pytest.mark.parametrize("swallow", [False, True])
async def test_metadata_check_deadline_cannot_mark_old_report_current(
    monkeypatch, swallow
):
    from maintainer_lens.clients.github import GitHubIssue
    from maintainer_lens.services.investigation_freshness import IssueObservation

    entered, cleaned, scopes = asyncio.Event(), asyncio.Event(), []
    real_timeout = asyncio.timeout

    def timeout(seconds):
        assert seconds == 15.0
        scope = real_timeout(seconds)
        scopes.append(scope)
        return scope

    monkeypatch.setattr(freshness, "asyncio", SimpleNamespace(timeout=timeout))
    now = datetime(2026, 10, 29, tzinfo=UTC)
    observation = IssueObservation(
        GitHubIssue(
            number=123,
            title="Issue",
            body=None,
            state="open",
            html_url="https://github.com/org/repo/issues/123",
            created_at=now,
            updated_at=now,
        ),
        "a" * 40,
        now,
    )

    async def observer(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not swallow:
                raise
            return observation
        finally:
            cleaned.set()

    operation = asyncio.create_task(
        freshness.bounded_observation(observer, "org/repo", 123)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        scopes[0].reschedule(asyncio.get_running_loop().time())
        value, error = await asyncio.wait_for(operation, 2)
        assert value is None and error.status == "unknown" and cleaned.is_set()
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


def test_truncated_analysis_has_an_actionable_safe_reason_and_remains_invalid():
    from maintainer_lens.agents.investigation_analysis import (
        AnalysisGenerationIncompleteError,
    )

    limited = AnalysisGenerationIncompleteError("length")
    assert isinstance(limited, ValueError)
    assert describe_failure(limited, retryable=False).code == "analysis_output_limit"
    unknown = AnalysisGenerationIncompleteError("fake-secret-provider-value")
    assert unknown.finish_reason == "unknown"
    assert "fake-secret" not in str(unknown)
    assert describe_failure(unknown, retryable=False).code == "analysis_incomplete"


@pytest.mark.parametrize(
    "message_generation,current_generation,ignored",
    [(0, 1, True), (1, 2, True), (2, 1, False)],
)
async def test_late_generation_is_ignored_and_future_generation_is_rejected(
    monkeypatch, message_generation, current_generation, ignored
):
    from unittest.mock import MagicMock

    import maintainer_lens.messaging.handler as handler

    monkeypatch.setattr(
        handler,
        "execute_investigation_task",
        AsyncMock(side_effect=runner.InvestigationClaimUnavailableError("unavailable")),
    )
    monkeypatch.setattr(
        handler,
        "get_investigation_task_generation",
        AsyncMock(return_value=current_generation),
    )
    status = AsyncMock()
    monkeypatch.setattr(handler, "get_investigation_task_status", status)
    request = InvestigationRequestedMessage(
        task_id="task",
        repository="org/repo",
        issue_number=123,
        generation=message_generation,
    )
    operation = handler.process_investigation_request(request, MagicMock(), AsyncMock())
    if ignored:
        await operation
    else:
        with pytest.raises(runner.PermanentInvestigationError):
            await operation
    status.assert_not_awaited()


def test_exported_report_keeps_unknown_freshness_qualification():
    report = render_observed_status(
        "# Saved analysis",
        "en",
        state="closed",
        checked_at=datetime(2026, 10, 29, tzinfo=UTC),
        freshness=InvestigationFreshness(status="unknown"),
    )
    assert "Closed" in report and "applicability is unknown" in report
    assert "unlinked" in report and report.endswith("# Saved analysis")


def test_compose_api_and_worker_compare_the_same_nonsecret_execution_identity():
    import re
    from pathlib import Path

    import yaml

    from maintainer_lens.core.config import Settings
    from maintainer_lens.services.investigation_configuration import (
        make_worker_execution_config,
    )

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "compose.yaml").read_text()
    )
    services = compose["services"]
    values = {
        "LLM_BASE_URL": "https://llm.example.test/v1",
        "LLM_MODEL_NAME": "analysis-test",
        "EMBEDDING_BASE_URL": "https://embed.example.test/v1",
        "EMBEDDING_MODEL": "embedding-test",
        "EMBEDDING_DIMENSIONS": "3",
    }

    def profile(service):
        environment = services[service]["environment"]
        configuration = {}
        for name in values:
            expression = re.fullmatch(r"\$\{([A-Z_]+):-([^}]*)\}", environment[name])
            assert expression is not None and expression[1] == name
            configuration[name.lower()] = values[name]
        settings = Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://test:test@localhost/test",
            redis_url="redis://localhost/0",
            rabbitmq_url="amqp://test:test@localhost/",
            llm_api_key=None,
            embedding_api_key=None,
            github_token=None,
            **configuration,
        )
        return make_worker_execution_config(settings, source_enabled=True)

    assert profile("api") == profile("worker")
    for name in ("LLM_API_KEY", "EMBEDDING_API_KEY"):
        assert name not in services["api"]["environment"]
        assert name in services["worker"]["environment"]
    assert (
        services["api"]["environment"]["GITHUB_TOKEN"]
        == services["worker"]["environment"]["GITHUB_TOKEN"]
    )
