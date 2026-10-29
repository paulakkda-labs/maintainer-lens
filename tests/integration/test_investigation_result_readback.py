"""HTTP result decoding and opt-in PostgreSQL readback; no provider/broker calls.

Default tests use isolated FastAPI apps without lifespan/settings. Database
tests require RUN_POSTGRES_INTEGRATION=1 and the already applied result migration.
Shared task fixtures own cascading cleanup; tests never create/migrate tables.
"""

import asyncio
import os
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

import maintainer_lens.api.routes.investigations as routes
from maintainer_lens.api.dependencies import get_db_session
from maintainer_lens.db.investigation_queries import get_investigation_details
from maintainer_lens.db.investigation_results import (
    StoredInvestigationResultError,
    decode_investigation_result,
)
from maintainer_lens.db.models.investigation import (
    InvestigationIssueSnapshot,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_reuse import InvestigationFreshness

pytestmark = pytest.mark.anyio
requires_postgres = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration tests are disabled",
)
INDEX_ID = "12345678-1234-4234-8234-123456789abc"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _payload(*, needs_more: bool = False, empty: bool = False) -> dict:
    history_url = "https://github.com/fastapi/fastapi/issues/124"
    source_url = f"https://github.com/fastapi/fastapi/blob/{'a' * 40}/client.py#L2-L4"
    source_id = f"source:{INDEX_ID}:{'c' * 64}"
    return {
        "repository": "fastapi/fastapi",
        "issue_number": 123,
        "issue_title": "Cancellation cleanup",
        "issue_body": None,
        "response_language": "zh",
        "source_index_id": None if empty else INDEX_ID,
        "next_step": "needs_more_evidence"
        if needs_more or empty
        else "draft_preliminary_report",
        "preliminary_analysis": {
            "summary": "证据不足。" if empty else "找到相关证据。",
            "findings": []
            if empty
            else [
                {
                    "statement": "存在清理路径。",
                    "evidence_ids": ["history:item:124", source_id],
                }
            ],
            "hypotheses": [],
            "unanswered_questions": ["Which path applies?"]
            if needs_more or empty
            else [],
        },
        "history_evidence": []
        if empty
        else [
            {
                "repository": "fastapi/fastapi",
                "number": 124,
                "source_type": "item",
                "comment_id": None,
                "title": "Historical cleanup",
                "content": "Later updated historical content. " * 100,
                "source_url": history_url,
                "source_updated_at": "2026-10-28T00:00:00+00:00",
                "distance": 0.12,
            }
        ],
        "source_evidence": []
        if empty
        else [
            {
                "index_id": INDEX_ID,
                "chunk_key": "c" * 64,
                "repository": "fastapi/fastapi",
                "commit_sha": "a" * 40,
                "blob_sha": "b" * 40,
                "file_path": "client.py",
                "language": "python",
                "kind": "code",
                "symbol": "cleanup",
                "start_line": 2,
                "end_line": 4,
                "content": "async def cleanup():\n    await release()\n" * 100,
                "source_url": source_url,
                "rrf_score": 0.03,
                "vector_rank": 1,
                "keyword_rank": None,
                "cosine_distance": 0.12,
                "matched_in": None,
            }
        ],
        # Saved analysis excerpts may differ from later returned History text.
        "analysis_evidence": []
        if empty
        else [
            {
                "evidence_id": "history:item:124",
                "category": "history",
                "source_url": history_url,
                "content": "Historical excerpt actually sent to the analyzer.",
            },
            {
                "evidence_id": source_id,
                "category": "source",
                "source_url": source_url,
                "content": "client.py:2-4\nasync def cleanup(): ...",
            },
        ],
        "search_round": 1 if empty else 3,
        "routing_reason": "No evidence." if empty else "Investigation finished.",
    }


def _record(payload=None, *, task_id="task-001", version=1):
    return InvestigationResultRecord(
        task_id=task_id,
        schema_version=version,
        result_payload=_payload() if payload is None else payload,
    )


def _task(status="succeeded", *, task_id="task-001"):
    return InvestigationTask(
        id=task_id,
        repository="fastapi/fastapi",
        issue_number=123,
        status=status,
        source_index_id=UUID(INDEX_ID),
        knowledge_budget=make_knowledge_preparation_budget().model_dump(),
    )


def _app(session) -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router)

    async def provide_session():
        yield session

    app.dependency_overrides[get_db_session] = provide_session
    return app


async def _get(app, task_id="task-001"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(f"/investigations/{task_id}")


@pytest.mark.parametrize(
    "needs_more,empty", [(False, False), (True, False), (True, True)]
)
async def test_http_returns_complete_typed_result(monkeypatch, needs_more, empty):
    payload = _payload(needs_more=needs_more, empty=empty)
    original = deepcopy(payload)
    session = object()
    task = _task()
    task.source_index_id = None if empty else UUID(INDEX_ID)
    query = AsyncMock(return_value=(task, None, _record(payload)))
    monkeypatch.setattr(routes, "get_investigation_details", query)

    response = await _get(_app(session))

    assert response.status_code == 200
    body = response.json()
    report = body.pop("report_markdown")
    assert body == {
        "task_id": "task-001",
        "repository": "fastapi/fastapi",
        "issue_number": 123,
        "status": "succeeded",
        "issue": None,
        "result": original,
        "source_index_id": payload["source_index_id"],
        "knowledge_budget": make_knowledge_preparation_budget().model_dump(),
        "auto_prepare_knowledge": False,
        "prepared_source_index_id": None,
        "generation": 0,
        "progress": None,
        "last_error": None,
        "freshness": InvestigationFreshness().model_dump(mode="json"),
        "previous_report_task_id": None,
        "previous_report_markdown": None,
    }
    assert isinstance(report, str) and "调查报告" in report
    assert payload == original
    query.assert_awaited_once_with(session, "task-001")
    decoded = decode_investigation_result(
        _record(payload),
        task_id="task-001",
        repository="fastapi/fastapi",
        issue_number=123,
        source_index_id=None if empty else UUID(INDEX_ID),
    )
    assert decoded.source_index_id == (None if empty else UUID(INDEX_ID))


@pytest.mark.parametrize("status", ["pending", "running", "failed"])
async def test_http_hides_unfinished_artifacts_without_decoding(monkeypatch, status):
    # Even an unsupported/corrupt old attempt must not become completed output.
    query = AsyncMock(return_value=(_task(status), None, _record({}, version=999)))
    monkeypatch.setattr(routes, "get_investigation_details", query)
    decoder = Mock(side_effect=AssertionError("Must not decode"))
    monkeypatch.setattr(routes, "decode_investigation_result", decoder)
    response = await _get(_app(object()))
    assert response.status_code == 200
    assert response.json()["result"] is None
    assert response.json()["report_markdown"] is None
    decoder.assert_not_called()


@pytest.mark.parametrize("status", ["pending", "running", "failed", "succeeded"])
async def test_missing_artifact_is_legal_for_legacy_tasks(monkeypatch, status):
    monkeypatch.setattr(
        routes,
        "get_investigation_details",
        AsyncMock(return_value=(_task(status), None, None)),
    )
    response = await _get(_app(object()))
    assert response.status_code == 200
    assert response.json()["result"] is None
    assert response.json()["report_markdown"] is None


def _corrupt_record(case):
    record = _record()
    payload = record.result_payload
    if case == "version":
        record.schema_version = 2
    elif case == "boolean_version":
        record.schema_version = True
    elif case == "task":
        record.task_id = "other-task"
    elif case == "repository":
        payload["repository"] = "other/repository"
    elif case == "issue":
        payload["issue_number"] = 999
    elif case == "source_identity":
        payload["source_index_id"] = str(UUID(int=1))
    elif case == "missing":
        del payload["analysis_evidence"]
    elif case == "analysis":
        payload["preliminary_analysis"]["findings"] = [{"statement": "bad"}]
    elif case == "internal_state":
        payload["searched_queries"] = ["secret query"]
    elif case == "numeric_string":
        payload["search_round"] = "1"
    elif case == "boolean_round":
        payload["search_round"] = True
    elif case == "round_budget":
        payload["search_round"] = 4
    elif case == "terminal":
        payload["next_step"] = "retry"
    elif case == "uuid":
        payload["source_index_id"] = "invalid-uuid"
    elif case == "nonfinite":
        payload["history_evidence"][0]["distance"] = float("nan")
    elif case == "non_json":
        payload["issue_body"] = object()
    elif case == "shape":
        record.result_payload = []
    else:
        raise AssertionError(case)
    return record


CORRUPTION_CASES = [
    "version",
    "boolean_version",
    "task",
    "repository",
    "issue",
    "source_identity",
    "missing",
    "analysis",
    "internal_state",
    "numeric_string",
    "boolean_round",
    "round_budget",
    "terminal",
    "uuid",
    "nonfinite",
    "non_json",
    "shape",
]


@pytest.mark.parametrize("case", CORRUPTION_CASES)
async def test_corrupt_storage_raises_typed_error_and_safe_http_500(monkeypatch, case):
    record = _corrupt_record(case)
    with pytest.raises(StoredInvestigationResultError):
        decode_investigation_result(
            record,
            task_id="task-001",
            repository="fastapi/fastapi",
            issue_number=123,
            source_index_id=UUID(INDEX_ID),
        )
    monkeypatch.setattr(
        routes,
        "get_investigation_details",
        AsyncMock(return_value=(_task(), None, record)),
    )
    response = await _get(_app(object()))
    assert response.status_code == 500
    assert response.json() == {"detail": "Stored investigation result is unavailable"}


async def test_query_is_one_read_statement_and_preserves_optional_rows():
    row = (_task(), None, _record())
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row))
    )
    assert await get_investigation_details(session, "task-001") == row
    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    sql = str(statement)
    assert sql.count("LEFT OUTER JOIN") == 2
    assert "investigation_results" in sql
    assert statement.is_select
    assert statement._for_update_arg is None
    assert statement.get_execution_options()["populate_existing"] is True

    session.execute.return_value.one_or_none = lambda: None
    assert await get_investigation_details(session, "missing") is None


async def test_query_db_errors_and_cancellation_propagate_unchanged():
    for error in [SQLAlchemyError("database failure"), asyncio.CancelledError()]:
        session = SimpleNamespace(execute=AsyncMock(side_effect=error))
        with pytest.raises(type(error)) as caught:
            await get_investigation_details(session, "task-001")
        assert caught.value is error


async def test_http_query_failure_closes_dependency_without_becoming_storage_error(
    monkeypatch,
):
    error = SQLAlchemyError("private database details")
    monkeypatch.setattr(
        routes, "get_investigation_details", AsyncMock(side_effect=error)
    )
    app = _app(object())
    closed = []

    async def session_dependency():
        try:
            yield object()
        finally:
            closed.append(True)

    app.dependency_overrides[get_db_session] = session_dependency
    with pytest.raises(SQLAlchemyError) as caught:
        await _get(app)
    assert caught.value is error
    assert closed == [True]


async def test_http_cancellation_propagates_and_closes_dependency(monkeypatch):
    started = asyncio.Event()
    cleaned = []

    async def blocked_query(*args):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned.append("query")

    async def session_dependency():
        try:
            yield object()
        finally:
            cleaned.append("session")

    monkeypatch.setattr(routes, "get_investigation_details", blocked_query)
    app = _app(object())
    app.dependency_overrides[get_db_session] = session_dependency
    request = asyncio.create_task(_get(app))
    try:
        async with asyncio.timeout(3):
            await started.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert cleaned == ["query", "session"]
    finally:
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def _seed(factory, task_id, *, status="succeeded", payload=None, version=1):
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(
                    status=status,
                    source_index_id=(
                        UUID(payload["source_index_id"])
                        if payload and payload.get("source_index_id")
                        else None
                    ),
                )
            )
            session.add(
                InvestigationIssueSnapshot(
                    task_id=task_id,
                    title="Snapshot title",
                    body=None,
                    state="open",
                    source_url="https://github.com/fastapi/fastapi/issues/123",
                    source_created_at=datetime(2026, 10, 1, tzinfo=UTC),
                    source_updated_at=datetime(2026, 10, 2, tzinfo=UTC),
                )
            )
            if payload is not None:
                session.add(_record(payload, task_id=task_id, version=version))


def _database_app(factory):
    app = _app(None)

    async def session_dependency():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = session_dependency
    return app


@requires_postgres
@pytest.mark.parametrize(
    "needs_more,empty", [(False, False), (True, False), (True, True)]
)
async def test_postgres_http_complete_round_trip(investigation_task, needs_more, empty):
    factory, task_id, _ = investigation_task
    payload = _payload(needs_more=needs_more, empty=empty)
    await _seed(factory, task_id, payload=payload)
    response = await _get(_database_app(factory), task_id)
    assert response.status_code == 200
    data = response.json()
    assert data["issue"]["title"] == "Snapshot title"
    assert data["result"] == payload


@requires_postgres
async def test_postgres_task_without_artifacts_is_readable(investigation_task):
    factory, task_id, _ = investigation_task
    response = await _get(_database_app(factory), task_id)
    assert response.status_code == 200
    assert response.json() == {
        "task_id": task_id,
        "repository": "fastapi/fastapi",
        "issue_number": 123,
        "status": "pending",
        "issue": None,
        "result": None,
        "source_index_id": None,
        "report_markdown": None,
        "knowledge_budget": make_knowledge_preparation_budget().model_dump(),
        "auto_prepare_knowledge": False,
        "prepared_source_index_id": None,
        "generation": 0,
        "progress": None,
        "last_error": None,
        "freshness": InvestigationFreshness().model_dump(mode="json"),
        "previous_report_task_id": None,
        "previous_report_markdown": None,
    }


@requires_postgres
@pytest.mark.parametrize("status", ["pending", "running", "failed", "succeeded"])
async def test_postgres_missing_result_preserves_task_and_snapshot(
    investigation_task, status
):
    factory, task_id, _ = investigation_task
    await _seed(factory, task_id, status=status)
    app = _database_app(factory)
    response = await _get(app, task_id)
    assert response.status_code == 200
    assert response.json()["status"] == status
    assert response.json()["issue"]["title"] == "Snapshot title"
    assert response.json()["result"] is None
    assert response.json()["report_markdown"] is None
    assert (await _get(app, "nonexistent-readback-task")).status_code == 404


@requires_postgres
@pytest.mark.parametrize("status", ["pending", "running", "failed", "succeeded"])
@pytest.mark.parametrize("corruption", ["version", "payload", "identity"])
async def test_postgres_corrupt_artifact_hidden_until_success(
    investigation_task, status, corruption
):
    factory, task_id, _ = investigation_task
    payload = _payload()
    if corruption == "payload":
        del payload["preliminary_analysis"]
    elif corruption == "identity":
        payload["repository"] = "other/repository"
    await _seed(
        factory,
        task_id,
        status=status,
        payload=payload,
        version=2 if corruption == "version" else 1,
    )
    response = await _get(_database_app(factory), task_id)
    if status == "succeeded":
        assert response.status_code == 500
        assert response.json() == {
            "detail": "Stored investigation result is unavailable"
        }
    else:
        assert response.status_code == 200
        assert response.json()["result"] is None
        assert response.json()["report_markdown"] is None


@requires_postgres
async def test_postgres_query_observes_committed_status_and_payload_together(
    investigation_task,
):
    factory, task_id, _ = investigation_task
    original = _payload()
    await _seed(factory, task_id, status="running", payload=original)
    replacement = _payload(needs_more=True)
    replacement["preliminary_analysis"]["summary"] = "Replacement committed result."

    async with factory() as reader:
        first = await get_investigation_details(reader, task_id)
        assert first[0].status == "running"
        assert first[2].result_payload == original
        await reader.rollback()

        async with factory() as writer:
            async with writer.begin():
                await writer.execute(
                    update(InvestigationTask)
                    .where(InvestigationTask.id == task_id)
                    .values(status="succeeded")
                )
                await writer.execute(
                    update(InvestigationResultRecord)
                    .where(InvestigationResultRecord.task_id == task_id)
                    .values(result_payload=replacement)
                )
                before_commit = await get_investigation_details(reader, task_id)
                assert before_commit[0].status == "running"
                assert before_commit[2].result_payload == original

            # Same request Session has cached ORM objects: refresh all artifacts.
            after_commit = await get_investigation_details(reader, task_id)
            assert after_commit[0].status == "succeeded"
            assert after_commit[2].result_payload == replacement

    response = await _get(_database_app(factory), task_id)
    assert response.status_code == 200
    assert response.json()["result"] == replacement
