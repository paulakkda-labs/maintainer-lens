"""Opt-in read-only URL lookup against unique PostgreSQL fixtures.

No Outbox records, provider requests or broker messages are created. Existing
services may stay running for this file alone. Cleanup deletes only owned tasks.
"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from maintainer_lens.api.dependencies import get_db_session
from maintainer_lens.api.routes.investigations import router
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.models.investigation_reuse import InvestigationTaskEvent
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
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


@pytest.fixture
async def scope(session_factory):
    suffix = uuid4().hex
    repository = f"lookup-tests/repo-{suffix}"
    prefix = f"lookup-{suffix}"
    ids = [f"{prefix}-{number}" for number in range(5)]
    now = datetime(2026, 10, 29, tzinfo=UTC)
    app = FastAPI()
    app.include_router(router)

    async def read_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = read_session
    try:
        async with session_factory() as session, session.begin():
            for number, task_id in enumerate(ids[:4]):
                session.add(
                    InvestigationTask(
                        id=task_id,
                        repository=repository.upper() if number == 1 else repository,
                        issue_number=200,
                        status=["pending", "failed", "succeeded", "waiting_review"][
                            number
                        ],
                        execution_generation=number,
                        auto_prepare_knowledge=number % 2 == 0,
                        knowledge_budget=make_knowledge_preparation_budget(
                            "small" if number % 2 else "standard"
                        ).model_dump(mode="json"),
                        # IDs 0/1 share a time so keyset tie-breaking is exercised.
                        created_at=now + timedelta(seconds=max(0, number - 1)),
                        updated_at=now,
                    )
                )
            session.add(
                InvestigationTask(
                    id=ids[4], repository=f"other-{repository}", issue_number=200
                )
            )
            await session.flush()
            session.add(
                InvestigationTaskEvent(
                    task_id=ids[1],
                    generation=1,
                    kind="failed",
                    phase="analyzing",
                    search_round=1,
                    error_payload={
                        "code": "invalid_data",
                        "message": "Investigation data failed validation.",
                        "retryable": False,
                    },
                )
            )
        yield app, session_factory, repository, ids, now
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id.in_(ids))
            )


async def _rows(factory, ids):
    async with factory() as session:
        return (
            await session.execute(
                select(
                    InvestigationTask.id,
                    InvestigationTask.status,
                    InvestigationTask.execution_generation,
                    InvestigationTask.updated_at,
                    InvestigationTask.claim_token,
                    InvestigationTask.user_recovery_count,
                )
                .where(InvestigationTask.id.in_(ids))
                .order_by(InvestigationTask.id)
            )
        ).all()


async def test_postgres_lookup_pages_case_insensitively_and_reads_existing_error(scope):
    app, factory, repository, ids, _ = scope
    before = await _rows(factory, ids)
    params = {"issue_url": f"https://github.com/{repository}/issues/200", "limit": 2}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        first = await client.get("/investigations/lookup", params=params)
        assert first.status_code == 200
        assert [task["task_id"] for task in first.json()["tasks"]] == [ids[3], ids[2]]
        second = await client.get(
            "/investigations/lookup",
            params={**params, "before": first.json()["next_before"]},
        )
        assert second.status_code == 200
        assert [task["task_id"] for task in second.json()["tasks"]] == [ids[1], ids[0]]
        assert second.json()["next_before"] is None
        detail = await client.get(f"/investigations/{ids[1]}")
        events = await client.get(f"/investigations/{ids[1]}/events")
        assert detail.status_code == events.status_code == 200
        assert detail.json()["progress"]["phase"] == "analyzing"
        assert detail.json()["last_error"]["code"] == "invalid_data"
        assert events.json()["events"][0]["kind"] == "failed"
        foreign = await client.get(
            "/investigations/lookup", params={**params, "before": ids[4]}
        )
        assert foreign.status_code == 400
        unknown = await client.get(
            "/investigations/lookup",
            params={
                **params,
                "issue_url": f"https://github.com/{repository}/issues/201",
            },
        )
        assert unknown.status_code == 200 and unknown.json()["tasks"] == []
    assert await _rows(factory, ids) == before


async def test_postgres_keyset_excludes_newer_insertions_from_following_page(scope):
    app, factory, repository, ids, now = scope
    params = {"issue_url": f"https://github.com/{repository}/issues/200", "limit": 2}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        first = await client.get("/investigations/lookup", params=params)
        assert first.status_code == 200
        new_id = f"{ids[0]}-new"
        ids.append(new_id)
        async with factory() as session, session.begin():
            session.add(
                InvestigationTask(
                    id=new_id,
                    repository=repository,
                    issue_number=200,
                    created_at=now + timedelta(seconds=10),
                )
            )
        following = await client.get(
            "/investigations/lookup",
            params={**params, "before": first.json()["next_before"]},
        )
        assert following.status_code == 200
        assert [task["task_id"] for task in following.json()["tasks"]] == [
            ids[1],
            ids[0],
        ]
        refreshed = await client.get("/investigations/lookup", params=params)
        assert refreshed.status_code == 200
        assert refreshed.json()["tasks"][0]["task_id"] == new_id
