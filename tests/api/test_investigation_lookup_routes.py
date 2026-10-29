"""Offline URL lookup protocol and SELECT-only query boundaries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select

import maintainer_lens.api.routes.investigations as routes
from maintainer_lens.api.dependencies import get_db_session, get_session_factory
from maintainer_lens.db.investigation_queries import find_investigation_tasks

pytestmark = pytest.mark.anyio
NOW = datetime(2026, 10, 29, tzinfo=UTC)
URL = "https://github.com/Owner/Repo/issues/200"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api(monkeypatch):
    app, session = FastAPI(), object()
    app.include_router(routes.router)

    async def read_session():
        yield session

    def forbidden_factory():
        raise AssertionError("Lookup must not create/arbitrate/dispatch work")

    app.dependency_overrides[get_db_session] = read_session
    app.dependency_overrides[get_session_factory] = forbidden_factory
    forbidden = AsyncMock(side_effect=AssertionError("No submission/freshness I/O"))
    monkeypatch.setattr(routes, "resolve_investigation_url", forbidden)
    monkeypatch.setattr(routes, "create_investigation_submission", forbidden)
    query = AsyncMock(return_value=([], None))
    monkeypatch.setattr(routes, "find_investigation_tasks", query)
    return app, session, query, forbidden


def _task(task_id="task", status="failed"):
    return SimpleNamespace(
        id=task_id,
        status=status,
        execution_generation=2,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_lookup_returns_existing_task_locators_without_resolution_or_dispatch(
    api,
):
    app, session, query, forbidden = api
    query.return_value = ([_task(), _task("older", "succeeded")], "older")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/investigations/lookup", params={"issue_url": URL, "limit": 2}
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["repository"] == "Owner/Repo" and payload["issue_number"] == 200
    assert [item["task_id"] for item in payload["tasks"]] == ["task", "older"]
    assert payload["tasks"][0]["generation"] == 2 and payload["next_before"] == "older"
    assert "report_markdown" not in payload["tasks"][0]
    assert payload["tasks"][0]["created_at"].endswith("Z")
    query.assert_awaited_once_with(
        session, repository="Owner/Repo", issue_number=200, before=None, limit=2
    )
    forbidden.assert_not_awaited()


async def test_unknown_url_returns_an_empty_page_and_never_submits(api):
    app, _, query, forbidden = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/investigations/lookup", params={"issue_url": URL})
    assert response.status_code == 200
    assert response.json()["tasks"] == [] and response.json()["next_before"] is None
    query.assert_awaited_once()
    forbidden.assert_not_awaited()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"issue_url": "http://127.0.0.1/secret"},
        {"issue_url": "https://github.com/Owner/Repo/pull/200"},
        {"issue_url": "https://github.com/Owner/Repo/issues/200?token=fake-secret"},
        {"issue_url": "https://github.com/../Repo/issues/200"},
        {"issue_url": "https://github.com/Owner/Repo/issues/2147483648"},
        {"issue_url": URL, "limit": 0},
        {"issue_url": URL, "limit": 101},
        {"issue_url": URL, "before": ""},
        {"issue_url": URL, "before": "x" * 65},
    ],
)
async def test_invalid_lookup_fails_before_query_execution(api, params):
    app, _, query, forbidden = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/investigations/lookup", params=params)
    assert response.status_code == 422
    query.assert_not_awaited()
    forbidden.assert_not_awaited()


async def test_invalid_cursor_returns_safe_error_and_no_submission(api):
    app, _, query, forbidden = api
    query.side_effect = ValueError("private-diagnostic-must-not-escape")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/investigations/lookup", params={"issue_url": URL, "before": "other-task"}
        )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid investigation page"}
    forbidden.assert_not_awaited()


async def test_next_cursor_is_forwarded_to_the_scoped_query(api):
    app, session, query, _ = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/investigations/lookup", params={"issue_url": URL, "before": "older"}
        )
    assert response.status_code == 200
    query.assert_awaited_once_with(
        session, repository="Owner/Repo", issue_number=200, before="older", limit=20
    )


class _ReadSession:
    def __init__(self, tasks=(), cursor=None):
        self.statements, self.tasks, self.cursor = [], tasks, cursor

    async def execute(self, statement):
        assert isinstance(statement, Select)
        self.statements.append(statement)
        return SimpleNamespace(one_or_none=lambda: self.cursor)

    async def scalars(self, statement):
        assert isinstance(statement, Select)
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: self.tasks)

    def add(self, *args):
        raise AssertionError("Read-only lookup")

    commit = flush = begin = add


async def test_query_is_bounded_select_only_and_cursor_uses_created_at_with_tie_breaker():
    tasks = [_task("new"), _task("middle"), _task("old")]
    session = _ReadSession(tasks, SimpleNamespace(created_at=NOW, id="before"))
    selected, before = await find_investigation_tasks(
        session, repository="OWNER/REPO", issue_number=200, before="before", limit=2
    )
    assert selected == tasks[:2] and before == "middle"
    assert len(session.statements) == 2
    cursor, page = [
        stmt.compile(dialect=postgresql.dialect()) for stmt in session.statements
    ]
    for statement in (cursor, page):
        text = str(statement)
        assert "FOR UPDATE" not in text and "JOIN" not in text
        assert "lower(investigation_tasks.repository)" in text
        assert (
            "owner/repo" in statement.params.values()
            and 200 in statement.params.values()
        )
    assert "(investigation_tasks.created_at, investigation_tasks.id) <" in str(page)
    assert "created_at DESC, investigation_tasks.id DESC" in str(page)
    assert 3 in page.params.values()


async def test_foreign_or_unknown_cursor_is_rejected_before_reading_the_page():
    session = _ReadSession()
    with pytest.raises(ValueError, match="cursor"):
        await find_investigation_tasks(
            session, repository="owner/repo", issue_number=200, before="foreign"
        )
    assert len(session.statements) == 1


@pytest.mark.parametrize("limit", [True, 0, 101, 1.5, "20"])
async def test_internal_page_budget_is_validated_before_database_access(limit):
    session = Mock()
    with pytest.raises(ValueError):
        await find_investigation_tasks(
            session, repository="owner/repo", issue_number=1, limit=limit
        )
    assert not session.mock_calls
