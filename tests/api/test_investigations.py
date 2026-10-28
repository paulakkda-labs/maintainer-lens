"""Tests for investigation HTTP endpoints."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import maintainer_lens.api.routes.investigations as investigation_routes
from maintainer_lens.api.dependencies import (
    get_db_session,
    get_session_factory,
)
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage


def create_test_app() -> FastAPI:
    """Create an isolated application for investigation route tests."""
    app = FastAPI()
    app.include_router(investigation_routes.router)
    return app


@pytest.mark.anyio
async def test_create_investigation_returns_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept an investigation without running it synchronously."""

    app = create_test_app()
    factory = object()

    app.dependency_overrides[get_session_factory] = lambda: factory

    message = InvestigationRequestedMessage(
        task_id="task-001",
        repository="encode/httpx",
        issue_number=3712,
    )

    create_submission = AsyncMock(return_value=message)

    monkeypatch.setattr(
        investigation_routes,
        "create_investigation_submission",
        create_submission,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/investigations",
            json={
                "repository": "encode/httpx",
                "issue_number": 3712,
            },
        )

    assert response.status_code == 202
    assert response.json() == {
        "task_id": "task-001",
        "status": "pending",
    }

    create_submission.assert_awaited_once_with(
        factory,
        repository="encode/httpx",
        issue_number=3712,
    )


@pytest.mark.anyio
async def test_get_pending_investigation_without_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a pending investigation without an issue snapshot."""

    app = create_test_app()
    session = object()

    async def fake_session():
        yield session

    app.dependency_overrides[get_db_session] = fake_session

    task = SimpleNamespace(
        id="task-001",
        repository="encode/httpx",
        issue_number=3712,
        status="pending",
    )

    query = AsyncMock(return_value=(task, None, None))

    monkeypatch.setattr(
        investigation_routes,
        "get_investigation_details",
        query,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/investigations/task-001")

    assert response.status_code == 200
    assert response.json() == {
        "task_id": "task-001",
        "repository": "encode/httpx",
        "issue_number": 3712,
        "status": "pending",
        "issue": None,
        "result": None,
    }

    query.assert_awaited_once_with(session, "task-001")


@pytest.mark.anyio
async def test_get_completed_investigation_with_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return the persisted issue snapshot for a completed task."""

    app = create_test_app()
    session = object()

    async def fake_session():
        yield session

    app.dependency_overrides[get_db_session] = fake_session

    task = SimpleNamespace(
        id="task-001",
        repository="encode/httpx",
        issue_number=3712,
        status="succeeded",
    )

    snapshot = SimpleNamespace(
        title="Example issue",
        body="Issue description",
        state="open",
        source_url="https://github.com/encode/httpx/issues/3712",
        source_created_at=datetime(2026, 10, 1, tzinfo=UTC),
        source_updated_at=datetime(2026, 10, 2, tzinfo=UTC),
        fetched_at=datetime(2026, 10, 20, tzinfo=UTC),
    )

    monkeypatch.setattr(
        investigation_routes,
        "get_investigation_details",
        AsyncMock(return_value=(task, snapshot, None)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/investigations/task-001")

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "succeeded"
    assert data["result"] is None
    assert data["issue"]["title"] == "Example issue"
    assert data["issue"]["source_url"] == snapshot.source_url
    assert data["issue"]["fetched_at"] == "2026-10-20T00:00:00Z"


@pytest.mark.anyio
async def test_get_unknown_investigation_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return 404 when an investigation task does not exist."""

    app = create_test_app()
    session = object()

    async def fake_session():
        yield session

    app.dependency_overrides[get_db_session] = fake_session

    monkeypatch.setattr(
        investigation_routes,
        "get_investigation_details",
        AsyncMock(return_value=None),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/investigations/missing-task")

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Investigation not found",
    }
