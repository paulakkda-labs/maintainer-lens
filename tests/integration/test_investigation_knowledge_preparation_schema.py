"""Preparation-mode schema contracts; opt-in PostgreSQL, no provider or MQ calls.

Default checks inspect registered metadata and render PostgreSQL migration DDL
without loading Alembic env/settings. Apply the revision manually before enabling
RUN_POSTGRES_INTEGRATION=1. These tests never apply migrations or expose the
future automatic preparation path through API/Worker.
"""

import importlib.util
import json
import os
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Boolean, CheckConstraint, Uuid, insert, inspect, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

import maintainer_lens.db.models as model_registry
from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    create_investigation_task,
    mark_investigation_failed,
    mark_investigation_succeeded,
    release_investigation_task_for_retry,
    renew_investigation_lease,
)
from maintainer_lens.db.models import Base
from maintainer_lens.db.models.investigation import InvestigationTask

pytestmark = pytest.mark.anyio
requires_postgres = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration tests are disabled",
)
REVISION = "e8b3c7d9a204"
REQUEST_CHECK = "ck_investigation_tasks_auto_source_request"
PREPARED_CHECK = "ck_investigation_tasks_prepared_source_mode"
INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
BUDGET_CHECK = "ck_investigation_tasks_knowledge_budget_object"
STANDARD_BUDGET = {
    "schema_version": 1,
    "profile": "standard",
    "profile_version": 1,
    "source_max_files": 500,
    "source_max_bytes": 8388608,
    "source_max_chunks": 2000,
    "history_max_items": 3,
    "preparation_timeout_seconds": 900,
}
BUDGET_DEFAULT_SQL = (
    "'" + json.dumps(STANDARD_BUDGET, separators=(",", ": ")) + "'::jsonb"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / f"{REVISION}_add_investigation_knowledge_preparation.py"
    )
    spec = importlib.util.spec_from_file_location(
        "knowledge_preparation_migration", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_existing_task_registration_includes_legacy_safe_preparation_fields():
    assert model_registry.InvestigationTask is InvestigationTask
    assert "InvestigationTask" in model_registry.__all__
    table = Base.metadata.tables["investigation_tasks"]
    assert table is InvestigationTask.__table__
    budget = table.c.knowledge_budget
    assert isinstance(budget.type, JSONB) and not budget.nullable
    assert str(budget.server_default.arg) == BUDGET_DEFAULT_SQL
    assert budget.server_default.arg.compile().params == {}
    assert budget.default is None
    mode = table.c.auto_prepare_knowledge
    assert isinstance(mode.type, Boolean) and not mode.nullable
    assert mode.default is None
    assert str(mode.server_default.arg) == "false"
    prepared = table.c.prepared_source_index_id
    assert isinstance(prepared.type, Uuid)
    assert prepared.type.as_uuid and prepared.type.native_uuid
    assert (
        prepared.nullable
        and prepared.default is None
        and prepared.server_default is None
    )
    assert (
        not prepared.foreign_keys and not prepared.unique and not prepared.primary_key
    )
    assert all(
        "prepared_source_index_id" not in index.columns for index in table.indexes
    )
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert (
        checks[REQUEST_CHECK] == "NOT auto_prepare_knowledge OR source_index_id IS NULL"
    )
    assert checks[BUDGET_CHECK] == "jsonb_typeof(knowledge_budget) = 'object'"
    assert (
        checks[PREPARED_CHECK]
        == "auto_prepare_knowledge OR prepared_source_index_id IS NULL"
    )


def test_preparation_revision_extends_the_explicit_source_revision():
    migration = _migration()
    assert migration.revision == REVISION and migration.down_revision == "d7e9f2a4b601"
    assert migration.branch_labels is None and migration.depends_on is None


@pytest.mark.parametrize(
    "direction,expected",
    [
        (
            "upgrade",
            [
                "ALTER TABLE investigation_tasks ADD COLUMN auto_prepare_knowledge BOOLEAN DEFAULT false NOT NULL;",
                "ALTER TABLE investigation_tasks ADD COLUMN prepared_source_index_id UUID;",
                f"ALTER TABLE investigation_tasks ADD COLUMN knowledge_budget JSONB DEFAULT {BUDGET_DEFAULT_SQL} NOT NULL;",
                f"ALTER TABLE investigation_tasks ADD CONSTRAINT {REQUEST_CHECK} CHECK (NOT auto_prepare_knowledge OR source_index_id IS NULL);",
                f"ALTER TABLE investigation_tasks ADD CONSTRAINT {PREPARED_CHECK} CHECK (auto_prepare_knowledge OR prepared_source_index_id IS NULL);",
                f"ALTER TABLE investigation_tasks ADD CONSTRAINT {BUDGET_CHECK} CHECK (jsonb_typeof(knowledge_budget) = 'object');",
            ],
        ),
        (
            "downgrade",
            [
                f"ALTER TABLE investigation_tasks DROP CONSTRAINT {BUDGET_CHECK};",
                f"ALTER TABLE investigation_tasks DROP CONSTRAINT {PREPARED_CHECK};",
                f"ALTER TABLE investigation_tasks DROP CONSTRAINT {REQUEST_CHECK};",
                "ALTER TABLE investigation_tasks DROP COLUMN knowledge_budget;",
                "ALTER TABLE investigation_tasks DROP COLUMN prepared_source_index_id;",
                "ALTER TABLE investigation_tasks DROP COLUMN auto_prepare_knowledge;",
            ],
        ),
    ],
)
def test_migration_only_changes_preparation_columns_and_constraints(
    direction, expected
):
    migration = _migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": output,
        },
    )
    migration.op = Operations(context)
    getattr(migration, direction)()
    statements = [
        line.strip() for line in output.getvalue().splitlines() if line.strip()
    ]
    assert statements == expected


async def _load(factory, task_id):
    async with factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert task is not None
        return task


@requires_postgres
async def test_applied_database_schema_matches_mode_and_prepared_identity(db_engine):
    def read_schema(connection):
        inspector = inspect(connection)
        return (
            inspector.get_columns("investigation_tasks"),
            inspector.get_check_constraints("investigation_tasks"),
            inspector.get_foreign_keys("investigation_tasks"),
        )

    async with db_engine.connect() as connection:
        columns, checks, foreign_keys = await connection.run_sync(read_schema)
    columns = {column["name"]: column for column in columns}
    mode = columns["auto_prepare_knowledge"]
    assert isinstance(mode["type"], Boolean) and not mode["nullable"]
    assert mode["default"].strip("'() ").lower() == "false"
    prepared = columns["prepared_source_index_id"]
    assert isinstance(prepared["type"], Uuid) and prepared["nullable"]
    assert prepared["default"] is None
    budget = columns["knowledge_budget"]
    assert isinstance(budget["type"], JSONB) and not budget["nullable"]
    assert (
        json.loads(budget["default"].removesuffix("::jsonb").strip("'() "))
        == STANDARD_BUDGET
    )
    assert {REQUEST_CHECK, PREPARED_CHECK, BUDGET_CHECK} <= {
        check["name"] for check in checks
    }
    assert all(
        "prepared_source_index_id" not in fk["constrained_columns"]
        for fk in foreign_keys
    )


@requires_postgres
async def test_existing_helper_keeps_legacy_mode_without_automatic_preparation(
    session_factory, task_id
):
    async with session_factory() as session, session.begin():
        await create_investigation_task(session, task_id, "encode/httpx", 123)
    task = await _load(session_factory, task_id)
    assert task.auto_prepare_knowledge is False
    assert task.prepared_source_index_id is None and task.source_index_id is None
    assert task.status == "pending"
    assert task.knowledge_budget == STANDARD_BUDGET


@requires_postgres
@pytest.mark.parametrize("requested_index", [None, INDEX_ID])
async def test_old_sql_inserts_receive_database_defaults(
    session_factory, task_id, requested_index
):
    async with session_factory() as session, session.begin():
        await session.execute(
            insert(InvestigationTask).values(
                id=task_id,
                repository="encode/httpx",
                issue_number=123,
                source_index_id=requested_index,
            )
        )
    task = await _load(session_factory, task_id)
    assert (
        task.auto_prepare_knowledge is False and task.prepared_source_index_id is None
    )
    assert task.source_index_id == requested_index
    assert task.knowledge_budget == STANDARD_BUDGET


@requires_postgres
@pytest.mark.parametrize("prepared_index", [None, INDEX_ID])
async def test_automatic_mode_can_be_unprepared_or_prepared_without_index_fk(
    session_factory, task_id, prepared_index
):
    async with session_factory() as session, session.begin():
        session.add(
            InvestigationTask(
                id=task_id,
                repository="encode/httpx",
                issue_number=123,
                auto_prepare_knowledge=True,
                prepared_source_index_id=prepared_index,
            )
        )
    task = await _load(session_factory, task_id)
    assert task.auto_prepare_knowledge is True and task.source_index_id is None
    assert task.prepared_source_index_id == prepared_index


@requires_postgres
@pytest.mark.parametrize(
    "fields,check_name",
    [
        ({"auto_prepare_knowledge": True, "source_index_id": INDEX_ID}, REQUEST_CHECK),
        (
            {"auto_prepare_knowledge": False, "prepared_source_index_id": INDEX_ID},
            PREPARED_CHECK,
        ),
    ],
)
async def test_database_rejects_conflicting_selection_modes(
    session_factory, task_id, fields, check_name
):
    with pytest.raises(IntegrityError, match=check_name):
        async with session_factory() as session, session.begin():
            await session.execute(
                insert(InvestigationTask).values(
                    id=task_id,
                    repository="encode/httpx",
                    issue_number=123,
                    **fields,
                )
            )
    async with session_factory() as session:
        assert await session.get(InvestigationTask, task_id) is None


@requires_postgres
async def test_database_rejects_null_preparation_mode(session_factory, task_id):
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await session.execute(
                insert(InvestigationTask).values(
                    id=task_id,
                    repository="encode/httpx",
                    issue_number=123,
                    auto_prepare_knowledge=None,
                )
            )


@requires_postgres
@pytest.mark.parametrize("terminal_status", ["succeeded", "failed"])
async def test_claim_renew_retry_and_terminal_updates_preserve_preparation_metadata(
    session_factory, task_id, terminal_status
):
    # Schema regression only: production preparation/binding will fence separately.
    async with session_factory() as session, session.begin():
        session.add(
            InvestigationTask(
                id=task_id,
                repository="encode/httpx",
                issue_number=123,
                auto_prepare_knowledge=True,
                prepared_source_index_id=INDEX_ID,
            )
        )
    async with session_factory() as session, session.begin():
        first = await claim_investigation_task(session, task_id)
        assert first is not None
    async with session_factory() as session, session.begin():
        assert await renew_investigation_lease(session, task_id, first)
        assert await release_investigation_task_for_retry(session, task_id, first)
    task = await _load(session_factory, task_id)
    assert task.status == "pending" and task.auto_prepare_knowledge is True
    assert task.prepared_source_index_id == INDEX_ID
    async with session_factory() as session, session.begin():
        second = await claim_investigation_task(session, task_id)
        assert second is not None and second != first
    finish = (
        mark_investigation_succeeded
        if terminal_status == "succeeded"
        else mark_investigation_failed
    )
    async with session_factory() as session, session.begin():
        assert not await finish(session, task_id, first)
        assert await finish(session, task_id, second)
    task = await _load(session_factory, task_id)
    assert task.status == terminal_status and task.auto_prepare_knowledge is True
    assert task.source_index_id is None and task.prepared_source_index_id == INDEX_ID
    assert task.claim_token is None and task.lease_expires_at is None


@requires_postgres
async def test_prepared_identity_update_obeys_caller_rollback(session_factory, task_id):
    async with session_factory() as session, session.begin():
        session.add(
            InvestigationTask(
                id=task_id,
                repository="encode/httpx",
                issue_number=123,
                auto_prepare_knowledge=True,
                prepared_source_index_id=INDEX_ID,
            )
        )
    async with session_factory() as session:
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(prepared_source_index_id=uuid4())
        )
        await session.rollback()
    task = await _load(session_factory, task_id)
    assert (
        task.prepared_source_index_id == INDEX_ID
        and task.auto_prepare_knowledge is True
    )
