"""Source identity propagation and deterministic report boundaries, without I/O."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

import maintainer_lens.api.routes.investigations as routes
import maintainer_lens.services.investigation_processor as processor
import maintainer_lens.services.outbox_dispatcher as dispatcher
from maintainer_lens.api.dependencies import get_db_session, get_session_factory
from maintainer_lens.db.investigation_results import (
    StoredInvestigationResultError,
    decode_investigation_result,
    upsert_investigation_result,
)
from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.investigation_runner import PermanentInvestigationError
from maintainer_lens.messaging.publisher import (
    create_investigation_message,
    republish_investigation_message,
)
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_contracts import (
    CreateInvestigationRequest,
    InvestigationInput,
)
from maintainer_lens.services.investigation_execution import execute_graph_investigation
from maintainer_lens.services.investigation_report import render_investigation_report
from maintainer_lens.services.investigation_submission import (
    create_investigation_submission,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)

pytestmark = pytest.mark.anyio
INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Factory:
    def __init__(self):
        self.rows = []
        self.active = 0
        self.committed = False
        self.flush = AsyncMock()

    @asynccontextmanager
    async def __call__(self):
        yield SimpleNamespace(add=self.rows.append, flush=self.flush, begin=self.begin)

    @asynccontextmanager
    async def begin(self):
        self.active += 1
        try:
            yield SimpleNamespace()
            self.committed = True
        finally:
            self.active -= 1


@pytest.fixture
async def result():
    hit = RepositoryHistorySemanticHit(
        repository="encode/httpx",
        number=124,
        source_type="item",
        comment_id=None,
        title="Cleanup history",
        content="Original analyzed excerpt.",
        source_url="https://github.com/encode/httpx/issues/124",
        source_updated_at=datetime(2026, 10, 28, tzinfo=UTC),
        distance=0.1,
    )
    from maintainer_lens.agents.investigation_analysis import InvestigationAnalysis

    return await execute_graph_investigation(
        InvestigationInput(
            repository="encode/httpx",
            issue_number=123,
            issue_title="Cleanup issue",
            issue_body=None,
            response_language="zh",
        ),
        history_searcher=AsyncMock(return_value=(hit,)),
        analyzer=AsyncMock(
            return_value=InvestigationAnalysis(
                summary="找到历史讨论。",
                findings=[
                    {
                        "statement": "历史记录讨论清理。",
                        "evidence_ids": ["history:item:124"],
                    }
                ],
                hypotheses=[],
                unanswered_questions=[],
            )
        ),
        timeout_seconds=5,
    )


@pytest.mark.parametrize("selected", [None, INDEX_ID])
async def test_submission_preserves_uuid_in_task_outbox_and_persistent_message(
    selected,
):
    factory = _Factory()
    message = await create_investigation_submission(
        factory, repository="encode/httpx", issue_number=123, source_index_id=selected
    )
    task, event = factory.rows
    assert isinstance(task, InvestigationTask) and isinstance(
        event, InvestigationOutboxEvent
    )
    assert task.source_index_id == message.source_index_id == selected
    assert factory.committed and factory.active == 0
    assert event.payload == message.model_dump(mode="json")
    published = create_investigation_message(
        message.task_id, message.repository, message.issue_number, selected
    )
    assert InvestigationRequestedMessage.model_validate_json(published.body) == message
    exchange = SimpleNamespace(publish=AsyncMock())
    incoming = SimpleNamespace(
        body=published.body, message_id=message.task_id, content_type="application/json"
    )
    await republish_investigation_message(exchange, incoming, "retry", 1)
    assert exchange.publish.await_args.kwargs["message"].body == published.body


@pytest.mark.parametrize("selected", [None, str(INDEX_ID)])
async def test_post_passes_validated_source_choice_without_running_graph(
    monkeypatch, selected
):
    app = FastAPI()
    app.include_router(routes.router)
    factory = object()
    app.dependency_overrides[get_session_factory] = lambda: factory
    submission = AsyncMock(
        return_value=InvestigationRequestedMessage(
            task_id="task-001",
            repository="encode/httpx",
            issue_number=123,
            source_index_id=selected,
        )
    )
    monkeypatch.setattr(routes, "create_investigation_submission", submission)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/investigations",
            json={
                "repository": "encode/httpx",
                "issue_number": 123,
                "source_index_id": selected,
            },
        )
    assert response.status_code == 202
    submission.assert_awaited_once_with(
        factory,
        repository="encode/httpx",
        issue_number=123,
        source_index_id=INDEX_ID if selected else None,
        knowledge_budget_profile="standard",
    )


@pytest.mark.parametrize("invalid", [True, 7, "not-a-uuid", {}, []])
async def test_bad_source_uuid_is_rejected_before_submission(monkeypatch, invalid):
    for model in (CreateInvestigationRequest, InvestigationRequestedMessage):
        with pytest.raises(ValidationError):
            model.model_validate(
                {
                    "task_id": "task-001",
                    "repository": "encode/httpx",
                    "issue_number": 123,
                    "source_index_id": invalid,
                }
            )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_session_factory] = object
    submission = AsyncMock()
    monkeypatch.setattr(routes, "create_investigation_submission", submission)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/investigations",
            json={
                "repository": "encode/httpx",
                "issue_number": 123,
                "source_index_id": invalid,
            },
        )
    assert response.status_code == 422
    submission.assert_not_awaited()


def test_old_message_without_source_uuid_remains_history_only():
    message = InvestigationRequestedMessage.model_validate_json(
        '{"task_id":"old-task","repository":"encode/httpx","issue_number":123}'
    )
    assert message.source_index_id is None


async def test_outbox_dispatch_preserves_uuid_and_closes_claim_transaction(monkeypatch):
    factory = _Factory()
    message = InvestigationRequestedMessage(
        task_id="task-001",
        repository="encode/httpx",
        issue_number=123,
        source_index_id=INDEX_ID,
    )
    claimed = SimpleNamespace(
        id=uuid4(),
        task_id=message.task_id,
        event_type="investigation.requested",
        payload=message.model_dump(mode="json"),
        delivery_token="token",
    )
    monkeypatch.setattr(
        dispatcher, "claim_next_outbox_event", AsyncMock(return_value=claimed)
    )
    monkeypatch.setattr(
        dispatcher, "mark_outbox_published", AsyncMock(return_value=True)
    )

    async def publish(**kwargs):
        assert factory.active == 0
        assert kwargs["source_index_id"] == INDEX_ID

    publish_mock = AsyncMock(side_effect=publish)
    monkeypatch.setattr(dispatcher, "publish_investigation", publish_mock)
    assert await dispatcher.dispatch_one_outbox_event(factory, object())
    publish_mock.assert_awaited_once()


@pytest.mark.parametrize(
    "stored,requested", [(INDEX_ID, None), (None, INDEX_ID), (INDEX_ID, UUID(int=1))]
)
async def test_processor_rejects_source_identity_mismatch_before_external_work(
    monkeypatch, stored, requested
):
    identity = AsyncMock(return_value=("encode/httpx", 123, stored))
    github = AsyncMock()
    checker = AsyncMock()
    monkeypatch.setattr(processor, "get_claimed_investigation_identity", identity)
    monkeypatch.setattr(processor, "investigate_issue", github)
    with pytest.raises(PermanentInvestigationError, match="task identity"):
        await processor.process_graph_investigation(
            http_client=object(),
            session_factory=_Factory(),
            task_id="task-001",
            claim_token="token",
            repository="encode/httpx",
            issue_number=123,
            response_language="zh",
            source_index_id=requested,
            source_searcher=AsyncMock(),
            source_index_validator=checker,
            history_searcher=AsyncMock(),
            analyzer=AsyncMock(),
            timeout_seconds=5,
        )
    github.assert_not_awaited()
    checker.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [ValueError("Missing index"), RuntimeError("DB failed"), asyncio.CancelledError()],
)
async def test_source_preflight_failure_keeps_signal_and_prevents_network(
    monkeypatch, error
):
    factory = _Factory()
    monkeypatch.setattr(
        processor,
        "get_claimed_investigation_identity",
        AsyncMock(return_value=("encode/httpx", 123, INDEX_ID)),
    )
    github = AsyncMock()
    monkeypatch.setattr(processor, "investigate_issue", github)

    async def validate(repository, index_id):
        assert factory.active == 0
        assert (repository, index_id) == ("encode/httpx", INDEX_ID)
        raise error

    with pytest.raises(type(error)) as caught:
        await processor.process_graph_investigation(
            http_client=object(),
            session_factory=factory,
            task_id="task-001",
            claim_token="token",
            repository="encode/httpx",
            issue_number=123,
            response_language="zh",
            source_index_id=INDEX_ID,
            source_searcher=AsyncMock(),
            source_index_validator=validate,
            history_searcher=AsyncMock(),
            analyzer=AsyncMock(),
            timeout_seconds=5,
        )
    assert caught.value is error
    github.assert_not_awaited()


async def test_report_is_deterministic_and_uses_saved_context_not_final_content(result):
    original = deepcopy(result.model_dump())
    result.history_evidence[0]["content"] = "Later retrieval changed the text."
    report = render_investigation_report(result)
    assert report == render_investigation_report(result)
    assert "Original analyzed excerpt" in report
    assert "Later retrieval changed" not in report
    assert "https://github.com/encode/httpx/issues/124" in report
    assert "有引用支持的发现" in report and "待验证假设" in report
    assert result.analysis_evidence == original["analysis_evidence"]


async def test_report_keeps_insufficiency_and_escapes_untrusted_text(result):
    result = result.model_copy(update={"next_step": "needs_more_evidence"})
    result.preliminary_analysis.hypotheses = ["<script>alert(1)</script>"]
    result.preliminary_analysis.unanswered_questions = ["[click](javascript:alert(1))"]
    result.preliminary_analysis.summary = (
        "# injected heading\n![image](https://evil.example)"
    )
    report = render_investigation_report(result)
    assert "证据不足" in report and "&lt;script&gt;" in report
    assert "<script>" not in report and "![image](" not in report
    assert "[click](javascript:" not in report
    result = result.model_copy(update={"response_language": "en"})
    assert "Insufficient evidence" in render_investigation_report(result)


@pytest.mark.parametrize(
    "corruption", ["unknown_reference", "foreign_url", "context_category"]
)
async def test_report_reuses_provenance_validation(result, corruption):
    if corruption == "unknown_reference":
        result.preliminary_analysis.findings[0].evidence_ids = ["history:item:999"]
    elif corruption == "foreign_url":
        result.analysis_evidence[0]["source_url"] = "https://evil.example/"
    else:
        result.analysis_evidence[0]["category"] = "source"
    with pytest.raises(ValueError):
        render_investigation_report(result)


@pytest.mark.parametrize("selected", [INDEX_ID, None])
async def test_result_writer_and_decoder_fence_source_identity(result, selected):
    result = result.model_copy(update={"source_index_id": INDEX_ID})
    record = InvestigationResultRecord(
        task_id="task-001",
        schema_version=1,
        result_payload=result.model_dump(mode="json"),
    )
    if selected == INDEX_ID:
        assert (
            decode_investigation_result(
                record,
                task_id="task-001",
                repository="encode/httpx",
                issue_number=123,
                source_index_id=selected,
            ).source_index_id
            == INDEX_ID
        )
        return
    with pytest.raises(StoredInvestigationResultError):
        decode_investigation_result(
            record,
            task_id="task-001",
            repository="encode/httpx",
            issue_number=123,
            source_index_id=selected,
        )
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                one_or_none=lambda: ("encode/httpx", 123, selected)
            )
        ),
        scalar=AsyncMock(return_value="task-001"),
    )
    with pytest.raises(ValueError, match="task identity"):
        await upsert_investigation_result(
            session, task_id="task-001", claim_token="token", result=result
        )
    session.execute.assert_awaited_once()


async def test_report_rejects_false_ready_marker_using_existing_router(result):
    result.preliminary_analysis.unanswered_questions = ["Which path is used?"]
    with pytest.raises(ValueError, match="report input"):
        render_investigation_report(result)


async def test_get_maps_report_provenance_failure_to_safe_integrity_error(
    monkeypatch, result
):
    result.analysis_evidence[0]["source_url"] = "https://evil.example/private-text"
    record = InvestigationResultRecord(
        task_id="task-001",
        schema_version=1,
        result_payload=result.model_dump(mode="json"),
    )
    task = InvestigationTask(
        id="task-001",
        repository="encode/httpx",
        issue_number=123,
        source_index_id=None,
        knowledge_budget=make_knowledge_preparation_budget().model_dump(),
        auto_prepare_knowledge=False,
        prepared_source_index_id=None,
        status="succeeded",
    )
    monkeypatch.setattr(
        routes,
        "get_investigation_details",
        AsyncMock(return_value=(task, None, record)),
    )
    app = FastAPI()
    app.include_router(routes.router)

    async def session():
        yield object()

    app.dependency_overrides[get_db_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/investigations/task-001")
    assert response.status_code == 500
    assert response.json() == {"detail": "Stored investigation result is unavailable"}
