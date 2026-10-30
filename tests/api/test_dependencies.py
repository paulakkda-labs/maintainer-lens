"""Tests for FastAPI database session dependencies."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.api.dependencies import get_db_session


def test_db_session_is_request_scoped() -> None:
    """Verify that each request receives and closes its own session."""
    app = FastAPI()

    created_sessions: list[object] = []
    received_sessions: list[object] = []
    closed_sessions: list[object] = []

    @asynccontextmanager
    async def fake_session_factory() -> AsyncGenerator[object, None]:
        session = object()
        created_sessions.append(session)

        try:
            yield session
        finally:
            closed_sessions.append(session)

    app.state.session_factory = fake_session_factory

    @app.get("/test-session")
    async def test_endpoint(
        session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, str]:
        received_sessions.append(session)
        return {"status": "ok"}

    with TestClient(app) as client:
        for _ in range(2):
            response = client.get("/test-session")

            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

    assert len(created_sessions) == 2
    assert created_sessions[0] is not created_sessions[1]

    assert received_sessions == created_sessions
    assert closed_sessions == created_sessions
