"""Budget profile contracts, immutable submission snapshots and opt-in SQL checks.

Default cases use a fake submission Session and real API/schema code; no settings,
providers or broker. PostgreSQL cases require a manually applied e8b3c7d9a204.
Budget selection is staged metadata; automatic preparation execution is separate.
"""

import json
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError

import maintainer_lens.api.routes.investigations as routes
import maintainer_lens.services.investigation_submission as submission
from maintainer_lens.api.dependencies import get_db_session, get_session_factory
from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    release_investigation_task_for_retry,
)
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.schemas.investigation_budget import (
    KnowledgePreparationBudget,
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_contracts import (
    CreateInvestigationRequest,
    InvestigationInput,
    InvestigationResult,
)

pytestmark = pytest.mark.anyio
requires_postgres = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration tests are disabled",
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Factory:
    def __init__(self):
        self.rows = []
        self.committed = False
        self.closed = False
        self.flush = AsyncMock()

    @asynccontextmanager
    async def __call__(self):
        try:
            yield SimpleNamespace(
                add=self.rows.append, flush=self.flush, begin=self.begin
            )
        finally:
            self.closed = True

    @asynccontextmanager
    async def begin(self):
        yield
        self.committed = True


def _app(factory):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_session_factory] = lambda: factory

    async def session():
        yield object()

    app.dependency_overrides[get_db_session] = session
    return app


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("small", (20, 262144, 100, 1, 300)),
        ("standard", (500, 8388608, 2000, 3, 900)),
        ("large", (2000, 8388608, 4000, 5, 1800)),
        ("unlimited", (None, None, None, None, None)),
    ],
)
def test_profiles_resolve_to_explicit_versioned_values(profile, expected):
    budget = make_knowledge_preparation_budget(profile)
    assert budget.schema_version == budget.profile_version == 1
    assert budget.profile == profile
    assert (
        budget.source_max_files,
        budget.source_max_bytes,
        budget.source_max_chunks,
        budget.history_max_items,
        budget.preparation_timeout_seconds,
    ) == expected
    encoded = budget.model_dump(mode="json")
    assert (
        KnowledgePreparationBudget.model_validate_json(
            json.dumps(encoded, allow_nan=False)
        )
        == budget
    )
    with pytest.raises(ValidationError, match="frozen_instance"):
        budget.profile_version = 2


def test_request_default_and_graph_contract_remain_separate():
    request = CreateInvestigationRequest(repository="encode/httpx", issue_number=123)
    assert request.knowledge_budget_profile == "standard"
    for model in (InvestigationInput, InvestigationResult):
        assert "knowledge_budget_profile" not in model.model_fields
        assert "knowledge_budget" not in model.model_fields
    with pytest.raises(ValidationError, match="extra_forbidden"):
        InvestigationInput(
            repository="encode/httpx",
            issue_number=123,
            issue_title="Cleanup",
            issue_body=None,
            response_language="zh",
            knowledge_budget_profile="unlimited",
        )


async def test_profile_discovery_is_read_only_and_precedes_task_detail_route(
    monkeypatch,
):
    query = AsyncMock(side_effect=AssertionError("Profiles must not read Task data"))
    monkeypatch.setattr(routes, "get_investigation_details", query)
    factory = Mock(side_effect=AssertionError("Profiles must not open a Session"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(_app(factory)), base_url="http://test"
    ) as client:
        response = await client.get("/investigations/budget-profiles")
    assert response.status_code == 200
    assert [item["profile"] for item in response.json()] == [
        "small",
        "standard",
        "large",
        "unlimited",
    ]
    assert response.json()[1] == make_knowledge_preparation_budget().model_dump(
        mode="json"
    )
    assert response.json()[-1]["preparation_timeout_seconds"] is None
    query.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("profile_version", 0),
        ("source_max_files", True),
        ("source_max_files", 0),
        ("source_max_files", -1),
        ("source_max_bytes", "8388608"),
        ("source_max_chunks", 1.5),
        ("history_max_items", False),
        ("preparation_timeout_seconds", 0),
        ("preparation_timeout_seconds", float("inf")),
        ("preparation_timeout_seconds", float("nan")),
        ("preparation_timeout_seconds", "unlimited"),
        ("source_max_chunks", None),
        ("unexpected", "internal"),
    ],
)
def test_budget_snapshots_reject_invalid_types_ranges_and_partial_unlimited(
    field, value
):
    payload = make_knowledge_preparation_budget().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        KnowledgePreparationBudget.model_validate(payload)


def test_unlimited_is_json_null_and_cannot_mix_in_finite_limits():
    payload = make_knowledge_preparation_budget("unlimited").model_dump(mode="json")
    assert all(
        payload[key] is None
        for key in (
            "source_max_files",
            "source_max_bytes",
            "source_max_chunks",
            "history_max_items",
            "preparation_timeout_seconds",
        )
    )
    payload["history_max_items"] = 3
    with pytest.raises(ValidationError, match="null task limits"):
        KnowledgePreparationBudget.model_validate(payload)


@pytest.mark.parametrize("profile", ["unknown", "STANDARD", "", None, True, 1, {}])
async def test_invalid_profile_fails_before_session_creation(profile):
    factory = Mock(side_effect=AssertionError("Must not open a Session"))
    with pytest.raises(ValueError, match="budget profile"):
        await submission.create_investigation_submission(
            factory,
            repository="encode/httpx",
            issue_number=123,
            knowledge_budget_profile=profile,
        )
    factory.assert_not_called()


@pytest.mark.parametrize("profile", ["small", "standard", "large", "unlimited"])
async def test_post_persists_snapshot_and_get_reads_it_without_resolving_again(
    monkeypatch, profile
):
    factory = _Factory()
    app = _app(factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/investigations",
            json={
                "repository": "encode/httpx",
                "issue_number": 123,
                "knowledge_budget_profile": profile,
                "knowledge_budget": {"source_max_files": 999999},
            },
        )
        assert response.status_code == 202
        task, event = factory.rows
        expected = make_knowledge_preparation_budget(profile).model_dump(mode="json")
        assert factory.committed and factory.closed
        assert task.knowledge_budget == expected
        assert (
            "knowledge_budget" not in event.payload
            and "knowledge_budget_profile" not in event.payload
        )
        monkeypatch.setattr(
            routes,
            "get_investigation_details",
            AsyncMock(return_value=(task, None, None)),
        )
        progress = AsyncMock(return_value=(None, None))
        monkeypatch.setattr(routes, "read_latest_progress", progress)
        monkeypatch.setattr(
            submission,
            "make_knowledge_preparation_budget",
            Mock(side_effect=AssertionError("GET must use the stored values")),
        )
        detail = await client.get(f"/investigations/{task.id}")
        assert detail.status_code == 200
        assert detail.json()["knowledge_budget"] == expected
        assert (
            detail.json()["result"] is None and detail.json()["report_markdown"] is None
        )
        assert detail.json()["progress"] is None and detail.json()["last_error"] is None
        progress.assert_awaited_once_with(
            ANY, task.id, generation=0, task_status="pending"
        )


@pytest.mark.parametrize("invalid", ["typo", None, True, 3, {}])
async def test_post_rejects_bad_profile_before_submission(monkeypatch, invalid):
    create = AsyncMock()
    monkeypatch.setattr(routes, "create_investigation_submission", create)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(_app(object())), base_url="http://test"
    ) as client:
        response = await client.post(
            "/investigations",
            json={
                "repository": "encode/httpx",
                "issue_number": 123,
                "knowledge_budget_profile": invalid,
            },
        )
    assert response.status_code == 422
    create.assert_not_awaited()


async def test_readback_preserves_a_historical_profile_version_and_values(monkeypatch):
    stored = make_knowledge_preparation_budget().model_dump()
    stored.update(profile_version=7, source_max_files=750)
    task = InvestigationTask(
        id="old-task",
        repository="encode/httpx",
        issue_number=123,
        status="pending",
        source_index_id=None,
        knowledge_budget=deepcopy(stored),
        auto_prepare_knowledge=False,
        prepared_source_index_id=None,
    )
    monkeypatch.setattr(
        routes, "get_investigation_details", AsyncMock(return_value=(task, None, None))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(_app(object())), base_url="http://test"
    ) as client:
        response = await client.get("/investigations/old-task")
    assert response.status_code == 200 and response.json()["knowledge_budget"] == stored
    assert task.knowledge_budget == stored


@pytest.mark.parametrize("corrupt", [None, {}, [], {"private": "not-for-response"}])
async def test_invalid_stored_budget_yields_safe_integrity_error(monkeypatch, corrupt):
    task = InvestigationTask(
        id="task-001",
        repository="encode/httpx",
        issue_number=123,
        status="pending",
        source_index_id=None,
        knowledge_budget=corrupt,
        auto_prepare_knowledge=False,
        prepared_source_index_id=None,
    )
    monkeypatch.setattr(
        routes, "get_investigation_details", AsyncMock(return_value=(task, None, None))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(_app(object())), base_url="http://test"
    ) as client:
        response = await client.get("/investigations/task-001")
    assert response.status_code == 500
    assert response.json() == {"detail": "Stored investigation budget is unavailable"}


@requires_postgres
@pytest.mark.parametrize("profile", ["small", "standard", "large", "unlimited"])
async def test_profile_snapshot_is_durable_and_survives_claim_and_retry(
    session_factory, task_id, monkeypatch, profile
):
    monkeypatch.setattr(submission, "uuid4", lambda: UUID(task_id))
    await submission.create_investigation_submission(
        session_factory,
        repository="encode/httpx",
        issue_number=123,
        knowledge_budget_profile=profile,
    )
    expected = make_knowledge_preparation_budget(profile).model_dump(mode="json")
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert task.knowledge_budget == expected
    async with session_factory() as session, session.begin():
        token = await claim_investigation_task(session, task_id)
        assert token is not None
    async with session_factory() as session, session.begin():
        assert await release_investigation_task_for_retry(session, task_id, token)
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert task.status == "pending" and task.knowledge_budget == expected


@requires_postgres
@pytest.mark.parametrize("payload", [None, [], "text", 7])
async def test_database_budget_requires_a_json_object(
    session_factory, task_id, payload
):
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await session.execute(
                insert(InvestigationTask).values(
                    id=task_id,
                    repository="encode/httpx",
                    issue_number=123,
                    knowledge_budget=payload,
                )
            )


@requires_postgres
async def test_snapshot_update_obeys_caller_rollback(session_factory, task_id):
    async with session_factory() as session, session.begin():
        session.add(
            InvestigationTask(
                id=task_id,
                repository="encode/httpx",
                issue_number=123,
                knowledge_budget=make_knowledge_preparation_budget().model_dump(
                    mode="json"
                ),
            )
        )
    async with session_factory() as session:
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(
                knowledge_budget=make_knowledge_preparation_budget(
                    "unlimited"
                ).model_dump(mode="json")
            )
        )
        await session.rollback()
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert task.knowledge_budget == make_knowledge_preparation_budget().model_dump(
            mode="json"
        )
