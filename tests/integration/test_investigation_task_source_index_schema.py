"""Task Source identity schema and opt-in PostgreSQL lifecycle regressions.

Default tests render migration SQL without loading Alembic env or settings.
PostgreSQL cases require RUN_POSTGRES_INTEGRATION=1 after a manual upgrade.
Tests never apply migrations or contact GitHub, embeddings, LLMs or RabbitMQ.
"""

import importlib.util
import os
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Uuid, delete, inspect, select, update

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
from maintainer_lens.db.models.repository_index import RepositoryIndex

pytestmark = pytest.mark.anyio
requires_postgres = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration tests are disabled",
)
REVISION = "d7e9f2a4b601"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / f"{REVISION}_add_investigation_source_index_id.py"
    )
    spec = importlib.util.spec_from_file_location("task_source_index_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_optional_uuid_is_registered_without_retention_coupling() -> None:
    assert model_registry.InvestigationTask is InvestigationTask
    assert "InvestigationTask" in model_registry.__all__
    table = Base.metadata.tables["investigation_tasks"]
    assert table is InvestigationTask.__table__
    column = table.c.source_index_id
    assert isinstance(column.type, Uuid)
    assert column.type.as_uuid and column.type.native_uuid
    assert column.nullable
    assert column.default is None and column.server_default is None
    assert not column.foreign_keys
    assert not column.primary_key and not column.unique
    assert all("source_index_id" not in index.columns for index in table.indexes)


def test_migration_extends_existing_result_revision() -> None:
    migration = _migration()
    assert migration.revision == REVISION
    assert migration.down_revision == "c4b2a6d8e901"
    assert migration.branch_labels is None and migration.depends_on is None


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (
            "upgrade",
            "ALTER TABLE investigation_tasks ADD COLUMN source_index_id UUID;",
        ),
        (
            "downgrade",
            "ALTER TABLE investigation_tasks DROP COLUMN source_index_id;",
        ),
    ],
)
def test_migration_renders_only_the_requested_column_change(
    direction, expected
) -> None:
    migration = _migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    migration.op = Operations(context)
    getattr(migration, direction)()
    assert output.getvalue().strip() == expected


async def _insert_selected_task(session_factory, task_id, index_id) -> None:
    async with session_factory() as session, session.begin():
        session.add(
            InvestigationTask(
                id=task_id,
                repository="encode/httpx",
                issue_number=123,
                source_index_id=index_id,
            )
        )


async def _read_task(session_factory, task_id):
    async with session_factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert task is not None
        return task


@requires_postgres
async def test_migrated_database_has_nullable_uuid_without_foreign_key(
    db_engine,
) -> None:
    def read_schema(connection):
        inspector = inspect(connection)
        return (
            inspector.get_columns("investigation_tasks"),
            inspector.get_foreign_keys("investigation_tasks"),
        )

    async with db_engine.connect() as connection:
        columns, foreign_keys = await connection.run_sync(read_schema)
    column = next(column for column in columns if column["name"] == "source_index_id")
    assert isinstance(column["type"], Uuid)
    assert column["nullable"] and column["default"] is None
    assert all(
        "source_index_id" not in fk["constrained_columns"] for fk in foreign_keys
    )


@requires_postgres
async def test_existing_creation_path_leaves_source_identity_null(
    session_factory, task_id
) -> None:
    async with session_factory() as session, session.begin():
        await create_investigation_task(session, task_id, "encode/httpx", 123)
    task = await _read_task(session_factory, task_id)
    assert task.source_index_id is None
    assert task.status == "pending"


@requires_postgres
@pytest.mark.parametrize("terminal_status", ["succeeded", "failed"])
async def test_selected_uuid_survives_claim_renew_retry_and_finish(
    session_factory, task_id, terminal_status
) -> None:
    index_id = uuid4()
    # A request identity may outlive (or refer to a missing) index; execution validates it.
    await _insert_selected_task(session_factory, task_id, index_id)
    async with session_factory() as session, session.begin():
        first_token = await claim_investigation_task(session, task_id)
        assert first_token is not None
    async with session_factory() as session, session.begin():
        assert await renew_investigation_lease(session, task_id, first_token)
        assert await release_investigation_task_for_retry(session, task_id, first_token)
    task = await _read_task(session_factory, task_id)
    assert task.status == "pending" and task.source_index_id == index_id
    async with session_factory() as session, session.begin():
        second_token = await claim_investigation_task(session, task_id)
        assert second_token is not None and second_token != first_token
    finish = (
        mark_investigation_succeeded
        if terminal_status == "succeeded"
        else mark_investigation_failed
    )
    async with session_factory() as session, session.begin():
        assert not await finish(session, task_id, first_token)
        assert await finish(session, task_id, second_token)
    task = await _read_task(session_factory, task_id)
    assert isinstance(task.source_index_id, UUID)
    assert task.source_index_id == index_id
    assert task.status == terminal_status
    assert task.claim_token is None and task.lease_expires_at is None


@requires_postgres
async def test_index_deletion_preserves_requested_identity(session_factory, task_id):
    index_id = uuid4()
    try:
        async with session_factory() as session, session.begin():
            session.add(
                RepositoryIndex(
                    id=index_id,
                    repository="encode/httpx",
                    commit_sha="a" * 40,
                    tree_sha="b" * 40,
                    embedding_provider="task-schema-test",
                    embedding_model=str(index_id),
                    embedding_dimensions=3,
                    chunker_version="task-schema-test",
                    status="ready",
                )
            )
        await _insert_selected_task(session_factory, task_id, index_id)
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(RepositoryIndex).where(RepositoryIndex.id == index_id)
            )
        task = await _read_task(session_factory, task_id)
        assert task.source_index_id == index_id and task.status == "pending"
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(RepositoryIndex).where(RepositoryIndex.id == index_id)
            )


@requires_postgres
async def test_request_field_update_obeys_caller_rollback(session_factory, task_id):
    index_id = uuid4()
    await _insert_selected_task(session_factory, task_id, index_id)
    async with session_factory() as session:
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(source_index_id=uuid4())
        )
        assert (
            await session.scalar(
                select(InvestigationTask.source_index_id).where(
                    InvestigationTask.id == task_id
                )
            )
            != index_id
        )
        await session.rollback()
    task = await _read_task(session_factory, task_id)
    assert task.source_index_id == index_id
