"""Result contracts and opt-in PostgreSQL fencing tests; no provider/broker calls.

Apply the Alembic migration manually before enabling RUN_POSTGRES_INTEGRATION.
Database tests use isolated tasks from the shared fixtures and cascading cleanup.
"""

import ast
import asyncio
import os
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import maintainer_lens.db.models as model_registry
from maintainer_lens.agents.investigation.state import InvalidInvestigationResultError
from maintainer_lens.db.investigation_results import upsert_investigation_result
from maintainer_lens.db.investigation_tasks import claim_investigation_task
from maintainer_lens.db.models import Base
from maintainer_lens.db.models import (
    InvestigationResultRecord as RegisteredInvestigationResultRecord,
)
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.models.investigation_result import (
    INVESTIGATION_RESULT_SCHEMA_VERSION,
    InvestigationResultRecord,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationResult

pytestmark = pytest.mark.anyio
requires_postgres = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration tests are disabled",
)

INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
REVISION = "c4b2a6d8e901"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _result(
    *, needs_more: bool = False, summary: str = "初步分析。"
) -> InvestigationResult:
    history_content = "Historical cancellation discussion. " * 100
    source_content = "async def cleanup():\n    await release()\n" * 100
    history_url = "https://github.com/fastapi/fastapi/issues/124#issuecomment-5"
    source_url = f"https://github.com/fastapi/fastapi/blob/{'a' * 40}/src/client%20request.py#L2-L4"
    source_id = f"source:{INDEX_ID}:{'c' * 64}"
    return InvestigationResult(
        repository="fastapi/fastapi",
        issue_number=123,
        issue_title="Cancellation cleanup",
        issue_body="Connections remain active after cancellation.",
        response_language="zh",
        source_index_id=INDEX_ID,
        next_step="needs_more_evidence" if needs_more else "draft_preliminary_report",
        preliminary_analysis={
            "summary": summary,
            "findings": [
                {
                    "statement": "找到了相关的历史讨论和源码。",
                    "evidence_ids": ["history:comment:5", source_id],
                }
            ],
            "hypotheses": [],
            "unanswered_questions": ["Which cleanup path is used?"]
            if needs_more
            else [],
        },
        history_evidence=[
            {
                "repository": "fastapi/fastapi",
                "number": 124,
                "source_type": "comment",
                "comment_id": 5,
                "title": "Cancellation discussion",
                "content": history_content,
                "source_url": history_url,
                "source_updated_at": "2026-10-28T00:00:00+00:00",
                "distance": 0.12,
            }
        ],
        source_evidence=[
            {
                "index_id": str(INDEX_ID),
                "chunk_key": "c" * 64,
                "repository": "fastapi/fastapi",
                "commit_sha": "a" * 40,
                "blob_sha": "b" * 40,
                "file_path": "src/client request.py",
                "language": "python",
                "kind": "code",
                "symbol": "cleanup",
                "start_line": 2,
                "end_line": 4,
                "content": source_content,
                "source_url": source_url,
                "rrf_score": 0.03,
                "vector_rank": 1,
                "keyword_rank": None,
                "cosine_distance": 0.12,
                "matched_in": None,
            }
        ],
        analysis_evidence=[
            {
                "evidence_id": "history:comment:5",
                "category": "history",
                "source_url": history_url,
                "content": f"Cancellation discussion\n{history_content[:1600]}",
            },
            {
                "evidence_id": source_id,
                "category": "source",
                "source_url": source_url,
                "content": f"src/client request.py:2-4\n{source_content[:1600]}",
            },
        ],
        search_round=2 if needs_more else 1,
        routing_reason="Unresolved question."
        if needs_more
        else "Cited findings, no gaps.",
    )


@pytest.fixture
async def leased_task(investigation_task):
    factory, task_id, engine = investigation_task
    async with factory() as session:
        async with session.begin():
            token = await claim_investigation_task(session, task_id)
    assert token is not None
    return factory, task_id, token, engine


async def _save(factory, task_id, token, result) -> bool:
    async with factory() as session:
        async with session.begin():
            return await upsert_investigation_result(
                session, task_id=task_id, claim_token=token, result=result
            )


async def _load(factory, task_id):
    async with factory() as session:
        return await session.get(InvestigationResultRecord, task_id)


async def _waiting_writer(factory, task_id, token, result, pid_future):
    """Expose the writer backend PID so tests can observe a real PostgreSQL wait."""
    async with factory() as session:
        async with session.begin():
            pid = await session.scalar(select(func.pg_backend_pid()))
            pid_future.set_result(pid)
            return await upsert_investigation_result(
                session, task_id=task_id, claim_token=token, result=result
            )


async def _wait_for_lock(factory, pid):
    async with asyncio.timeout(5):
        while True:
            # End each read transaction to avoid a cached statistics snapshot.
            async with factory() as session:
                wait_type = await session.scalar(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": pid},
                )
            if wait_type == "Lock":
                return
            await asyncio.sleep(0.01)


async def _wait_for_lease_expiry(factory, task_id):
    async with asyncio.timeout(5):
        while True:
            async with factory() as session:
                expired = await session.scalar(
                    select(
                        InvestigationTask.lease_expires_at <= func.clock_timestamp()
                    ).where(InvestigationTask.id == task_id)
                )
            if expired:
                return
            await asyncio.sleep(0.01)


def test_result_model_is_registered_in_shared_metadata() -> None:
    assert RegisteredInvestigationResultRecord is InvestigationResultRecord
    assert "InvestigationResultRecord" in model_registry.__all__
    table = Base.metadata.tables["investigation_results"]
    assert table is InvestigationResultRecord.__table__
    assert list(table.primary_key.columns.keys()) == ["task_id"]
    assert isinstance(table.c.result_payload.type, JSONB)
    foreign_key = next(iter(table.c.task_id.foreign_keys))
    assert foreign_key.target_fullname == "investigation_tasks.id"
    assert foreign_key.ondelete == "CASCADE"
    assert "status" not in table.c


def test_result_migration_extends_a_connected_single_head_chain() -> None:
    revisions = {}
    parents = set()
    directory = Path(__file__).resolve().parents[2] / "migrations" / "versions"
    for path in directory.glob("*.py"):
        values = {}
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id in ("revision", "down_revision"):
                    values[node.target.id] = ast.literal_eval(node.value)
        if "revision" not in values:
            continue
        assert values["revision"] not in revisions
        revisions[values["revision"]] = values["down_revision"]
        parent = values["down_revision"]
        if isinstance(parent, str):
            parents.add(parent)
        elif parent is not None:
            parents.update(parent)
    assert revisions[REVISION] == "789b1caf1332"
    assert parents <= revisions.keys()
    heads = revisions.keys() - parents
    assert len(heads) == 1
    # Later revisions may extend this migration without changing this test.
    ancestor = next(iter(heads))
    seen = set()
    while ancestor is not None:
        assert ancestor not in seen
        seen.add(ancestor)
        ancestor = revisions[ancestor]
    assert REVISION in seen


@pytest.mark.parametrize(
    "case",
    ["raw_dict", "extra_state", "bad_structure", "mutated_analysis", "nan", "infinity"],
)
async def test_invalid_result_is_rejected_before_any_sql(case) -> None:
    result = _result()
    if case == "raw_dict":
        result = result.model_dump()
    elif case == "extra_state":
        result = result.model_copy(update={"searched_queries": []})
    elif case == "bad_structure":
        result = result.model_copy(update={"next_step": "succeeded"})
    elif case == "mutated_analysis":
        result.preliminary_analysis.summary = ""
    else:
        result.history_evidence[0]["distance"] = (
            float("nan") if case == "nan" else float("inf")
        )
    session = AsyncMock(spec=AsyncSession)
    with pytest.raises(InvalidInvestigationResultError):
        await upsert_investigation_result(
            session, task_id="unused-task", claim_token="unused-token", result=result
        )
    session.execute.assert_not_awaited()
    session.scalar.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [RuntimeError("Database operation failed"), asyncio.CancelledError()]
)
async def test_sql_failure_or_cancellation_propagates_without_transaction_ownership(
    error,
):
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = error
    with pytest.raises(type(error)) as caught:
        await upsert_investigation_result(
            session, task_id="unused-task", claim_token="unused-token", result=_result()
        )
    assert caught.value is error
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@requires_postgres
async def test_migrated_result_table_matches_the_model(db_engine) -> None:
    def read_schema(connection):
        inspector = inspect(connection)
        return (
            inspector.get_columns("investigation_results"),
            inspector.get_pk_constraint("investigation_results"),
            inspector.get_foreign_keys("investigation_results"),
            inspector.get_check_constraints("investigation_results"),
        )

    async with db_engine.connect() as connection:
        columns, primary_key, foreign_keys, checks = await connection.run_sync(
            read_schema
        )
    assert {column["name"] for column in columns} == {
        "task_id",
        "schema_version",
        "result_payload",
        "created_at",
        "updated_at",
    }
    assert all(not column["nullable"] for column in columns)
    assert isinstance(
        next(
            column["type"] for column in columns if column["name"] == "result_payload"
        ),
        JSONB,
    )
    assert primary_key["constrained_columns"] == ["task_id"]
    assert any(
        key["referred_table"] == "investigation_tasks"
        and key["options"].get("ondelete") == "CASCADE"
        for key in foreign_keys
    )
    assert {check["name"] for check in checks} >= {
        "ck_investigation_results_schema_version_positive",
        "ck_investigation_results_payload_object",
    }


@requires_postgres
@pytest.mark.parametrize("needs_more", [False, True])
async def test_full_result_round_trip_does_not_change_task_status(
    leased_task, needs_more
):
    factory, task_id, token, _ = leased_task
    result = _result(needs_more=needs_more)

    assert await _save(factory, task_id, token, result)
    stored = await _load(factory, task_id)
    assert stored is not None
    assert stored.schema_version == INVESTIGATION_RESULT_SCHEMA_VERSION == 1
    assert stored.result_payload == result.model_dump(mode="json")
    assert InvestigationResult.model_validate(stored.result_payload) == result
    assert len(stored.result_payload["history_evidence"][0]["content"]) > 1600
    assert len(stored.result_payload["source_evidence"][0]["content"]) > 1600
    assert stored.result_payload["analysis_evidence"] == result.analysis_evidence
    assert stored.created_at.utcoffset() is not None
    assert stored.updated_at >= stored.created_at
    async with factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert task.status == "running"
        assert task.claim_token == token


@requires_postgres
async def test_upsert_replaces_one_row_and_preserves_creation_time(leased_task) -> None:
    factory, task_id, token, _ = leased_task
    assert await _save(factory, task_id, token, _result())
    original = await _load(factory, task_id)
    updated = _result(needs_more=True, summary="Updated analysis.")

    assert await _save(factory, task_id, token, updated)
    stored = await _load(factory, task_id)
    assert stored.result_payload == updated.model_dump(mode="json")
    assert stored.created_at == original.created_at
    assert stored.updated_at >= original.updated_at
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(InvestigationResultRecord)
            .where(InvestigationResultRecord.task_id == task_id)
        )
    assert count == 1


@requires_postgres
@pytest.mark.parametrize(
    "case",
    [
        "pending",
        "succeeded",
        "failed",
        "wrong_token",
        "expired",
        "null_lease",
        "null_token",
    ],
)
async def test_invalid_claim_cannot_overwrite_existing_result(leased_task, case):
    factory, task_id, token, _ = leased_task
    original = _result()
    assert await _save(factory, task_id, token, original)
    changes = {}
    supplied_token = token
    if case in ("pending", "succeeded", "failed"):
        changes["status"] = case
    elif case == "wrong_token":
        supplied_token = "another-token"
    elif case == "expired":
        changes["lease_expires_at"] = func.clock_timestamp() - timedelta(seconds=1)
    elif case == "null_lease":
        changes["lease_expires_at"] = None
    else:
        changes["claim_token"] = None
    if changes:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    update(InvestigationTask)
                    .where(InvestigationTask.id == task_id)
                    .values(**changes)
                )

    assert not await _save(
        factory, task_id, supplied_token, _result(summary="Rejected")
    )
    stored = await _load(factory, task_id)
    assert stored.result_payload == original.model_dump(mode="json")


@requires_postgres
async def test_missing_task_returns_false_without_inserting(leased_task) -> None:
    factory, _, token, _ = leased_task
    missing_id = f"missing-{uuid4().hex}"
    assert not await _save(factory, missing_id, token, _result())
    assert await _load(factory, missing_id) is None


@requires_postgres
@pytest.mark.parametrize(
    "change", [{"repository": "another/repo"}, {"issue_number": 999}]
)
async def test_result_identity_must_match_locked_task(leased_task, change) -> None:
    factory, task_id, token, _ = leased_task
    with pytest.raises(InvalidInvestigationResultError, match="task identity"):
        await _save(factory, task_id, token, _result().model_copy(update=change))
    assert await _load(factory, task_id) is None


@requires_postgres
async def test_result_write_is_not_visible_until_caller_commit(leased_task) -> None:
    factory, task_id, token, _ = leased_task
    async with factory() as session:
        async with session.begin():
            assert await upsert_investigation_result(
                session, task_id=task_id, claim_token=token, result=_result()
            )
            assert await _load(factory, task_id) is None
    assert await _load(factory, task_id) is not None


@requires_postgres
async def test_result_write_rolls_back_with_caller_transaction(leased_task) -> None:
    factory, task_id, token, _ = leased_task
    with pytest.raises(RuntimeError, match="rollback"):
        async with factory() as session:
            async with session.begin():
                assert await upsert_investigation_result(
                    session, task_id=task_id, claim_token=token, result=_result()
                )
                raise RuntimeError("Force caller rollback")
    assert await _load(factory, task_id) is None


@requires_postgres
@pytest.mark.parametrize("values", [{"schema_version": 0}, {"result_payload": []}])
async def test_database_rejects_invalid_version_or_payload_shape(leased_task, values):
    factory, task_id, _, _ = leased_task
    with pytest.raises(IntegrityError):
        async with factory() as session:
            async with session.begin():
                session.add(
                    InvestigationResultRecord(
                        **{
                            "task_id": task_id,
                            "schema_version": 1,
                            "result_payload": _result().model_dump(mode="json"),
                            **values,
                        }
                    )
                )
                await session.flush()
    assert await _load(factory, task_id) is None


@requires_postgres
async def test_task_deletion_cascades_to_its_result(leased_task) -> None:
    factory, task_id, token, _ = leased_task
    assert await _save(factory, task_id, token, _result())
    async with factory() as session:
        async with session.begin():
            await session.execute(
                delete(InvestigationTask).where(InvestigationTask.id == task_id)
            )
    assert await _load(factory, task_id) is None


@requires_postgres
async def test_waiting_old_owner_cannot_overwrite_replacement_result(leased_task):
    factory, task_id, old_token, _ = leased_task
    new_token = "replacement-owner"
    replacement = _result(summary="Replacement owner's result")
    pid_future = asyncio.get_running_loop().create_future()
    writer = None
    try:
        async with factory() as holder:
            async with holder.begin():
                await holder.execute(
                    update(InvestigationTask)
                    .where(InvestigationTask.id == task_id)
                    .values(claim_token=new_token)
                )
                writer = asyncio.create_task(
                    _waiting_writer(
                        factory,
                        task_id,
                        old_token,
                        _result(summary="Stale result"),
                        pid_future,
                    )
                )
                pid = await asyncio.wait_for(pid_future, timeout=5)
                await _wait_for_lock(factory, pid)
                assert not writer.done()
                assert await upsert_investigation_result(
                    holder, task_id=task_id, claim_token=new_token, result=replacement
                )
        assert await asyncio.wait_for(writer, timeout=5) is False
        stored = await _load(factory, task_id)
        assert stored.result_payload == replacement.model_dump(mode="json")
    finally:
        if writer is not None:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)


@requires_postgres
async def test_lease_expiring_while_waiting_for_lock_rejects_write(leased_task):
    factory, task_id, token, _ = leased_task
    async with factory() as session:
        async with session.begin():
            await session.execute(
                update(InvestigationTask)
                .where(InvestigationTask.id == task_id)
                .values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=3))
            )
    pid_future = asyncio.get_running_loop().create_future()
    writer = None
    try:
        async with factory() as holder:
            async with holder.begin():
                await holder.execute(
                    select(InvestigationTask.id)
                    .where(InvestigationTask.id == task_id)
                    .with_for_update()
                )
                writer = asyncio.create_task(
                    _waiting_writer(factory, task_id, token, _result(), pid_future)
                )
                pid = await asyncio.wait_for(pid_future, timeout=5)
                await _wait_for_lock(factory, pid)
                async with factory() as monitor:
                    assert (
                        await monitor.scalar(
                            select(
                                InvestigationTask.lease_expires_at
                                > func.clock_timestamp()
                            ).where(InvestigationTask.id == task_id)
                        )
                        is True
                    )
                # Let real database time expire the lease without updating the row.
                await _wait_for_lease_expiry(factory, task_id)
        assert await asyncio.wait_for(writer, timeout=5) is False
        assert await _load(factory, task_id) is None
    finally:
        if writer is not None:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)


@requires_postgres
async def test_payload_is_snapshotted_before_waiting_for_lock(leased_task):
    factory, task_id, token, _ = leased_task
    result = _result()
    expected = deepcopy(result.model_dump(mode="json"))
    pid_future = asyncio.get_running_loop().create_future()
    writer = None
    try:
        async with factory() as holder:
            async with holder.begin():
                await holder.execute(
                    select(InvestigationTask.id)
                    .where(InvestigationTask.id == task_id)
                    .with_for_update()
                )
                writer = asyncio.create_task(
                    _waiting_writer(factory, task_id, token, result, pid_future)
                )
                pid = await asyncio.wait_for(pid_future, timeout=5)
                await _wait_for_lock(factory, pid)
                result.preliminary_analysis.summary = "Changed after serialization"
                result.history_evidence[0]["content"] = "Changed after serialization"
        assert await asyncio.wait_for(writer, timeout=5) is True
        stored = await _load(factory, task_id)
        assert stored.result_payload == expected
    finally:
        if writer is not None:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)


@requires_postgres
async def test_cancellation_after_write_rolls_back_and_releases_task_lock(leased_task):
    factory, task_id, token, _ = leased_task
    written = asyncio.Event()

    async def uncommitted_writer():
        async with factory() as session:
            async with session.begin():
                assert await upsert_investigation_result(
                    session, task_id=task_id, claim_token=token, result=_result()
                )
                written.set()
                await asyncio.Event().wait()

    writer = asyncio.create_task(uncommitted_writer())
    try:
        await asyncio.wait_for(written.wait(), timeout=5)
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(writer, timeout=5)
        assert await _load(factory, task_id) is None
        # A new writer can acquire the released lock and commit normally.
        assert await asyncio.wait_for(
            _save(factory, task_id, token, _result(summary="After cancellation")),
            timeout=5,
        )
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
