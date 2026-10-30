"""Opt-in PostgreSQL fencing, rollback and result readback for automatic tasks.

Uses real compiled Graph with empty fake retrieval. No broker, GitHub or model
calls. Existing applied migrations are required; every task is independently
owned and removed by the shared task_id fixture.
"""

import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from maintainer_lens.agents.investigation.state import InvalidInvestigationResultError
from maintainer_lens.api.routes.investigations import router
from maintainer_lens.db.investigation_preparation import (
    bind_prepared_source_index,
    get_claimed_knowledge_preparation,
)
from maintainer_lens.db.investigation_results import upsert_investigation_result
from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    get_claimed_investigation_identity,
)
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationInput
from maintainer_lens.services.investigation_execution import execute_graph_investigation
from maintainer_lens.services.investigation_submission import (
    create_investigation_submission,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
        reason="PostgreSQL integration is disabled",
    ),
]
REPOSITORY = "automatic-binding/repository"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _task(factory, task_id, *, response_language="en"):
    async with factory() as session, session.begin():
        session.add(
            InvestigationTask(
                id=task_id,
                repository=REPOSITORY,
                issue_number=123,
                response_language=response_language,
                auto_prepare_knowledge=True,
                knowledge_budget=make_knowledge_preparation_budget(
                    "small"
                ).model_dump(),
                status="pending",
            )
        )
    async with factory() as session, session.begin():
        return await claim_investigation_task(session, task_id)


async def _bind(factory, task_id, token, index_id, **changes):
    async with factory() as session, session.begin():
        return await bind_prepared_source_index(
            session,
            task_id=task_id,
            claim_token=token,
            repository=changes.get("repository", REPOSITORY),
            issue_number=123,
            source_index_id=index_id,
        )


async def _result(index_id, *, response_language="en"):
    return await execute_graph_investigation(
        InvestigationInput(
            repository=REPOSITORY,
            issue_number=123,
            issue_title="Cleanup",
            issue_body=None,
            response_language=response_language,
            source_index_id=index_id,
        ),
        history_searcher=AsyncMock(return_value=()),
        source_searcher=AsyncMock(return_value=[]),
        analyzer=AsyncMock(),
        timeout_seconds=5,
    )


async def test_auto_submission_persists_only_backend_owned_preparation_fields(
    session_factory, task_id, monkeypatch
):
    from uuid import UUID

    import maintainer_lens.services.investigation_submission as submission

    monkeypatch.setattr(submission, "uuid4", lambda: UUID(task_id))
    message = await create_investigation_submission(
        session_factory,
        repository=REPOSITORY,
        issue_number=123,
        auto_prepare_knowledge=True,
        knowledge_budget_profile="small",
    )
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)
    assert message.auto_prepare_knowledge and task.auto_prepare_knowledge
    assert task.source_index_id is None and task.prepared_source_index_id is None
    assert (
        task.knowledge_budget == make_knowledge_preparation_budget("small").model_dump()
    )


@pytest.mark.parametrize("response_language", ["en", "zh"])
async def test_auto_mode_preflight_and_stored_budget_are_authoritative(
    session_factory, task_id, response_language
):
    token = await _task(session_factory, task_id, response_language=response_language)
    async with session_factory() as session, session.begin():
        assert (
            await get_claimed_investigation_identity(
                session,
                task_id=task_id,
                claim_token=token,
                response_language=response_language,
            )
            is None
        )
        assert await get_claimed_investigation_identity(
            session,
            task_id=task_id,
            claim_token=token,
            auto_prepare_knowledge=True,
            response_language=response_language,
        ) == (REPOSITORY, 123, None)
        with pytest.raises(ValueError, match="message language differs"):
            await get_claimed_investigation_identity(
                session,
                task_id=task_id,
                claim_token=token,
                auto_prepare_knowledge=True,
                response_language="zh" if response_language == "en" else "en",
            )
        context = await get_claimed_knowledge_preparation(
            session, task_id=task_id, claim_token=token
        )
        assert (
            context.budget.profile == "small"
            and context.prepared_source_index_id is None
        )


async def test_binding_is_idempotent_and_rejects_reselection_and_foreign_repository(
    session_factory, task_id
):
    token, index_id = await _task(session_factory, task_id), uuid4()
    assert await _bind(session_factory, task_id, token, index_id)
    assert await _bind(session_factory, task_id, token, index_id)
    assert not await _bind(session_factory, task_id, token, uuid4())
    assert not await _bind(
        session_factory, task_id, token, index_id, repository="foreign/repository"
    )
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert (
            task.prepared_source_index_id == index_id and task.source_index_id is None
        )


async def test_binding_rolls_back_with_caller_transaction(session_factory, task_id):
    token = await _task(session_factory, task_id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with session_factory() as session, session.begin():
            assert await bind_prepared_source_index(
                session,
                task_id=task_id,
                claim_token=token,
                repository=REPOSITORY,
                issue_number=123,
                source_index_id=uuid4(),
            )
            raise RuntimeError("rollback")
    async with session_factory() as session:
        assert (
            await session.get(InvestigationTask, task_id)
        ).prepared_source_index_id is None


@pytest.mark.parametrize("change", ["expired", "replaced", "terminal", "legacy"])
async def test_invalid_claim_or_mode_cannot_bind(session_factory, task_id, change):
    token = await _task(session_factory, task_id)
    values = {
        "expired": {"lease_expires_at": func.clock_timestamp() - timedelta(seconds=1)},
        "replaced": {"claim_token": "replacement"},
        "terminal": {"status": "failed"},
        "legacy": {"auto_prepare_knowledge": False},
    }[change]
    async with session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(**values)
        )
    assert not await _bind(session_factory, task_id, token, uuid4())


async def test_expiry_while_waiting_for_task_lock_cannot_bind(session_factory, task_id):
    token = await _task(session_factory, task_id)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(
                lease_expires_at=func.clock_timestamp() + timedelta(milliseconds=100)
            )
        )
    entered = asyncio.Event()

    async def bind():
        entered.set()
        return await _bind(session_factory, task_id, token, uuid4())

    async with session_factory() as blocker, blocker.begin():
        await blocker.execute(
            select(InvestigationTask.id)
            .where(InvestigationTask.id == task_id)
            .with_for_update()
        )
        operation = asyncio.create_task(bind())
        await entered.wait()
        await asyncio.sleep(0.2)
    try:
        assert not await asyncio.wait_for(operation, 5)
    finally:
        if not operation.done():
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation


@pytest.mark.parametrize("response_language", ["en", "zh"])
async def test_effective_index_fences_result_and_is_returned_by_get(
    session_factory, task_id, response_language
):
    token = await _task(session_factory, task_id, response_language=response_language)
    index_id = uuid4()
    assert await _bind(session_factory, task_id, token, index_id)
    result = await _result(index_id, response_language=response_language)
    async with session_factory() as session, session.begin():
        assert await upsert_investigation_result(
            session, task_id=task_id, claim_token=token, result=result
        )
    foreign = result.model_copy(update={"source_index_id": uuid4()})
    with pytest.raises(InvalidInvestigationResultError, match="identity"):
        async with session_factory() as session, session.begin():
            await upsert_investigation_result(
                session, task_id=task_id, claim_token=token, result=foreign
            )
    foreign_language = result.model_copy(
        update={"response_language": "zh" if response_language == "en" else "en"}
    )
    with pytest.raises(InvalidInvestigationResultError, match="identity"):
        async with session_factory() as session, session.begin():
            await upsert_investigation_result(
                session, task_id=task_id, claim_token=token, result=foreign_language
            )
    async with session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(status="succeeded", claim_token=None, lease_expires_at=None)
        )
    app = FastAPI()
    app.state.session_factory = session_factory
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(f"/investigations/{task_id}")
    assert response.status_code == 200
    body = response.json()
    assert (
        body["response_language"]
        == body["result"]["response_language"]
        == response_language
    )
    assert body["source_index_id"] is None and body["auto_prepare_knowledge"]
    assert (
        body["prepared_source_index_id"]
        == body["result"]["source_index_id"]
        == str(index_id)
    )
    assert (
        body["result"]["next_step"] == "needs_more_evidence" and body["report_markdown"]
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(prepared_source_index_id=uuid4())
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        corrupt = await client.get(f"/investigations/{task_id}")
    assert corrupt.status_code == 500
    assert corrupt.json() == {"detail": "Stored investigation result is unavailable"}


async def test_unbound_automatic_task_cannot_write_a_result(session_factory, task_id):
    token = await _task(session_factory, task_id)
    result = await _result(uuid4())
    async with session_factory() as session, session.begin():
        assert not await upsert_investigation_result(
            session, task_id=task_id, claim_token=token, result=result
        )
