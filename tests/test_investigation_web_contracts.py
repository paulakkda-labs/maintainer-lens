"""Offline Web serving, language defaults, submission and storage boundaries."""

import hashlib
import importlib.util
import json
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from pydantic import ValidationError

import maintainer_lens.api.routes.investigations as routes
import maintainer_lens.services.outbox_dispatcher as dispatcher
from maintainer_lens.agents.investigation.state import InvalidInvestigationResultError
from maintainer_lens.api.dependencies import get_session_factory
from maintainer_lens.db.investigation_results import upsert_investigation_result
from maintainer_lens.db.investigation_reuse import url_intent_key
from maintainer_lens.db.investigation_tasks import get_claimed_investigation_identity
from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.messaging.publisher import create_investigation_message
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_checkpoint import GraphCheckpointConfig
from maintainer_lens.schemas.investigation_contracts import (
    CreateInvestigationRequest,
    InvestigationResult,
)
from maintainer_lens.schemas.investigation_reuse import (
    InvestigationFreshness,
    ResolveInvestigationResponse,
)
from maintainer_lens.schemas.investigation_review import ReviewContext
from maintainer_lens.schemas.investigation_url import CreateInvestigationFromUrlRequest
from maintainer_lens.services.investigation_review import review_response
from maintainer_lens.services.investigation_submission import (
    create_investigation_submission,
)
from maintainer_lens.web import configure_web

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_public_defaults_are_english_and_old_message_payloads_remain_chinese():
    assert (
        CreateInvestigationRequest(
            repository="owner/repo", issue_number=1
        ).response_language
        == "en"
    )
    assert (
        CreateInvestigationFromUrlRequest(
            issue_url="https://github.com/owner/repo/issues/1"
        ).response_language
        == "en"
    )
    old = InvestigationRequestedMessage.model_validate_json(
        '{"task_id":"old","repository":"owner/repo","issue_number":1}'
    )
    assert old.response_language == "zh" and "response_language" not in old.model_dump()
    current = old.model_copy(update={"response_language": "en"})
    assert json.loads(current.model_dump_json())["response_language"] == "en"


@pytest.mark.parametrize("language", [None, True, "fr", "EN", ""])
def test_invalid_language_is_rejected_by_intake_and_message(language):
    with pytest.raises(ValidationError):
        CreateInvestigationFromUrlRequest(
            issue_url="https://github.com/owner/repo/issues/1",
            response_language=language,
        )
    with pytest.raises(ValidationError):
        InvestigationRequestedMessage(
            task_id="task",
            repository="owner/repo",
            issue_number=1,
            response_language=language,
        )


def test_chinese_lookup_hash_is_unchanged_and_english_has_a_separate_identity():
    budget = {"profile": "standard"}
    historical = hashlib.sha256(
        json.dumps(
            {"repository": "owner/repo", "issue_number": 1, "budget": budget},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert url_intent_key("OWNER/REPO", 1, budget, response_language="zh") == historical
    assert url_intent_key("owner/repo", 1, budget, response_language="en") != historical


async def test_dispatcher_preserves_english_in_the_actual_broker_payload(monkeypatch):
    payload = InvestigationRequestedMessage(
        task_id="task", repository="owner/repo", issue_number=1, response_language="en"
    )
    claimed = SimpleNamespace(
        id=1,
        delivery_token="token",
        task_id="task",
        generation=0,
        event_type="investigation.requested",
        payload=payload.model_dump(mode="json"),
    )

    @asynccontextmanager
    async def begin():
        yield object()

    monkeypatch.setattr(
        dispatcher, "claim_next_outbox_event", AsyncMock(return_value=claimed)
    )
    monkeypatch.setattr(
        dispatcher, "mark_outbox_published", AsyncMock(return_value=True)
    )
    publish = AsyncMock()
    monkeypatch.setattr(dispatcher, "publish_investigation", publish)
    assert await dispatcher.dispatch_one_outbox_event(
        SimpleNamespace(begin=begin), object()
    )
    kwargs = publish.await_args.kwargs.copy()
    kwargs.pop("exchange")
    message = create_investigation_message(**kwargs)
    assert (
        InvestigationRequestedMessage.model_validate_json(
            message.body
        ).response_language
        == "en"
    )


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_route_forwards_selected_language_without_acquiring_provider_resources(
    monkeypatch, language
):
    app = FastAPI()
    app.include_router(routes.router)
    factory = object()
    app.dependency_overrides[get_session_factory] = lambda: factory
    resolve = AsyncMock(
        return_value=ResolveInvestigationResponse(
            task_id="task",
            response_language=language,
            status="pending",
            decision="created",
            generation=0,
            freshness=InvestigationFreshness(),
        )
    )
    monkeypatch.setattr(routes, "resolve_investigation_url", resolve)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/investigations/from-url",
            json={
                "issue_url": "https://github.com/owner/repo/issues/1",
                "response_language": language,
            },
        )
    assert (
        response.status_code == 202 and response.json()["response_language"] == language
    )
    assert resolve.await_args.kwargs["response_language"] == language


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_submission_snapshots_task_and_outbox_language_in_one_transaction(
    language,
):
    added = []

    class Session:
        add = staticmethod(added.append)
        flush = AsyncMock()

        @asynccontextmanager
        async def begin(self):
            yield

    @asynccontextmanager
    async def factory():
        yield Session()

    message = await create_investigation_submission(
        factory, repository="owner/repo", issue_number=1, response_language=language
    )
    task, outbox = added
    assert isinstance(task, InvestigationTask) and isinstance(
        outbox, InvestigationOutboxEvent
    )
    assert task.response_language == message.response_language == language
    assert (
        InvestigationRequestedMessage.model_validate(outbox.payload).response_language
        == language
    )


async def test_claim_language_mismatch_fails_before_external_execution():
    row = SimpleNamespace(
        repository="owner/repo",
        issue_number=1,
        source_index_id=None,
        response_language="en",
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(one_or_none=lambda: row))
    )
    assert await get_claimed_investigation_identity(
        session, task_id="task", claim_token="claim", response_language="en"
    ) == ("owner/repo", 1, None)
    with pytest.raises(ValueError, match="language"):
        await get_claimed_investigation_identity(
            session, task_id="task", claim_token="claim", response_language="zh"
        )


def test_review_readback_rejects_another_task_language_before_exposing_actions():
    execution = GraphCheckpointConfig(
        response_language="en",
        max_search_rounds=1,
        timeout_seconds=5.0,
        search_strategy="deterministic",
        source_enabled=False,
        history_top_k=5,
        source_top_k=5,
        embedding_provider="test",
        embedding_model="embed",
        embedding_dimensions=3,
        embedding_endpoint_fingerprint="1" * 64,
        llm_model="model",
        llm_endpoint_fingerprint="2" * 64,
        llm_max_output_tokens=4096,
        thinking_enabled=False,
    )
    context = ReviewContext(
        reason="source_files",
        observed=21,
        knowledge_budget=make_knowledge_preparation_budget("small"),
        base_execution=execution,
        output_tokens=4096,
    )
    row = SimpleNamespace(
        context_payload=context.model_dump(mode="json"), decision_payload=None
    )
    with pytest.raises(ValueError, match="language"):
        review_response(row, SimpleNamespace(response_language="zh"))


async def test_result_writer_rejects_a_different_language_from_the_locked_task():
    result = InvestigationResult(
        repository="owner/repo",
        issue_number=1,
        issue_title="Question",
        issue_body=None,
        response_language="en",
        next_step="needs_more_evidence",
        preliminary_analysis={
            "summary": "Insufficient evidence",
            "findings": [],
            "hypotheses": [],
            "unanswered_questions": ["Which version?"],
        },
        history_evidence=[],
        source_evidence=[],
        analysis_evidence=[],
        search_round=1,
        routing_reason="No evidence",
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                one_or_none=lambda: ("owner/repo", 1, None, "zh")
            )
        ),
        scalar=AsyncMock(return_value="task"),
    )
    with pytest.raises(InvalidInvestigationResultError, match="identity"):
        await upsert_investigation_result(
            session, task_id="task", claim_token="claim", result=result
        )
    session.execute.assert_awaited_once()


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_language_migration_preserves_history_and_guards_downgrade(direction):
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/3cd7a9b1e528_add_investigation_response_language.py"
    )
    spec = importlib.util.spec_from_file_location("language_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "1bc6f8a0d417"
    output = StringIO()
    module.op = Operations(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
    )
    getattr(module, direction)()
    sql = output.getvalue()
    if direction == "upgrade":
        assert sql.index("DEFAULT 'zh'") < sql.index("SET DEFAULT 'en'")
        assert "response_language IN ('zh', 'en')" in sql
    else:
        assert "Non-Chinese investigation history prevents downgrade" in sql
        assert sql.index("RAISE EXCEPTION") < sql.index("DROP COLUMN")


async def test_web_routes_are_same_origin_and_do_not_swallow_api_or_asset_errors(
    tmp_path,
):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app.js").write_text("export {};")
    (tmp_path / "index.html").write_text(
        '<!doctype html><html lang="en"><body>MaintainerLens</body></html>'
    )
    app = FastAPI()

    @app.get("/investigations/example")
    async def api():
        return {"status": "running"}

    configure_web(app, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for path in ("/", "/logs"):
            response = await client.get(path)
            assert response.status_code == 200 and response.headers[
                "content-type"
            ].startswith("text/html")
            assert "script-src 'self'" in response.headers["content-security-policy"]
            assert response.headers["cache-control"] == "no-store"
        assert (await client.get("/investigations/example")).json() == {
            "status": "running"
        }
        assert (await client.get("/assets/app.js")).status_code == 200
        assert (await client.get("/assets/missing.js")).status_code == 404
        assert (await client.get("/unknown-page")).status_code == 404


async def test_missing_web_build_does_not_start_resources_or_hide_api_docs(tmp_path):
    app = FastAPI()
    configure_web(app, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/")).status_code == 503
        assert (await client.get("/docs")).status_code == 200
