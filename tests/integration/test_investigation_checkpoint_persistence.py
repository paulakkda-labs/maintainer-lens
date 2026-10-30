"""Opt-in real PostgreSQL checkpoint fencing/recovery; no HTTP or broker calls.

Run Alembic upgrade first. Every test owns a Task; cascading cleanup removes only
its run/checkpoints/writes. Provider dependencies are simulated, Graph is real.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.serde.types import ERROR
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.agents.investigation.checkpointing import (
    InvestigationCheckpointSaver,
)
from maintainer_lens.agents.investigation_analysis import InvestigationAnalysis
from maintainer_lens.db.investigation_checkpoints import (
    InvalidInvestigationCheckpointError,
    InvestigationCheckpointStore,
)
from maintainer_lens.db.investigation_results import upsert_investigation_result
from maintainer_lens.db.investigation_tasks import (
    claim_investigation_task,
    create_investigation_task,
)
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.models.investigation_checkpoint import (
    InvestigationCheckpoint,
    InvestigationCheckpointWrite,
    InvestigationGraphRun,
)
from maintainer_lens.db.models.investigation_result import InvestigationResultRecord
from maintainer_lens.investigation_runner import (
    LeaseLostError,
    RetryableInvestigationError,
    execute_investigation_task,
)
from maintainer_lens.schemas.investigation_checkpoint import (
    GraphCheckpointConfig,
    endpoint_fingerprint,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationInput
from maintainer_lens.services.investigation_checkpoint_execution import (
    execute_checkpointed_investigation,
    load_checkpointed_investigation,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
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


def _config():
    return GraphCheckpointConfig(
        response_language="en",
        max_search_rounds=3,
        timeout_seconds=10.0,
        search_strategy="deterministic",
        source_enabled=False,
        history_top_k=5,
        source_top_k=5,
        embedding_provider="test",
        embedding_model="embedding",
        embedding_dimensions=3,
        embedding_endpoint_fingerprint=endpoint_fingerprint(
            "https://embedding.example"
        ),
        llm_model="analysis",
        llm_endpoint_fingerprint=endpoint_fingerprint("https://llm.example"),
        llm_max_output_tokens=4096,
        thinking_enabled=False,
    )


def _input():
    return InvestigationInput(
        repository="fastapi/fastapi",
        issue_number=123,
        issue_title="Cleanup",
        issue_body="Check cancellation cleanup.",
        response_language="en",
    )


def _thread(task_id, checkpoint_id=None):
    configurable = {"thread_id": task_id, "checkpoint_ns": ""}
    if checkpoint_id is not None:
        configurable["checkpoint_id"] = checkpoint_id
    return {"configurable": configurable}


@pytest.fixture
async def owned(session_factory, task_id, db_engine):
    factory, engine = session_factory, db_engine
    async with factory() as session, session.begin():
        await create_investigation_task(
            session,
            task_id=task_id,
            repository="fastapi/fastapi",
            issue_number=123,
            response_language=_input().response_language,
        )
        token = await claim_investigation_task(session, task_id)
    assert token
    store = InvestigationCheckpointStore(factory, task_id=task_id, claim_token=token)
    run = await store.create_run(_input(), _config())
    return factory, task_id, engine, store, run


async def _expire(factory, task_id):
    async with factory() as session, session.begin():
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )


async def _reclaim(factory, task_id):
    await _expire(factory, task_id)
    async with factory() as session, session.begin():
        token = await claim_investigation_task(session, task_id)
    assert token
    return InvestigationCheckpointStore(factory, task_id=task_id, claim_token=token)


def _history():
    return (
        RepositoryHistorySemanticHit(
            repository="fastapi/fastapi",
            number=124,
            source_type="item",
            comment_id=None,
            title="Cleanup discussion",
            content="Release resources in finally.",
            source_url="https://github.com/fastapi/fastapi/issues/124",
            source_updated_at=datetime(2026, 10, 29, tzinfo=UTC),
            distance=0.1,
        ),
    )


def _analysis(request):
    return InvestigationAnalysis(
        summary="Preliminary result.",
        findings=[
            {
                "statement": "Cleanup is discussed.",
                "evidence_ids": [request["evidence"][0]["evidence_id"]],
            }
        ],
        hypotheses=[],
        unanswered_questions=[],
    )


@pytest.mark.parametrize("format_version", [2, 4])
async def test_postgres_round_trip_pending_writes_before_snapshot_and_immutable_frames(
    owned,
    format_version,
):
    factory, task_id, _, store, _ = owned
    frame = empty_checkpoint()
    frame["v"] = format_version
    config = _thread(task_id, frame["id"])
    saver = InvestigationCheckpointSaver(store)
    await saver.aput_writes(config, [("value", "first")], "node-z", "z")
    await saver.aput_writes(config, [("value", "ignored")], "node-z", "z")
    await saver.aput_writes(config, [(ERROR, "old")], "node-z", "z")
    await saver.aput_writes(config, [(ERROR, "new")], "node-z", "z")
    await saver.aput_writes(config, [("value", "earlier")], "node-a", "a")
    frame["channel_values"] = {"bytes": b"payload", "nested": [False, None, 1]}
    # Omitted input namespace is accepted; returned configs include the root ns.
    saved = await saver.aput(
        {"configurable": {"thread_id": task_id}}, frame, {"step": 0}, {}
    )
    assert saved == config
    restored = await InvestigationCheckpointSaver(store).aget_tuple(config)
    assert restored.checkpoint == frame
    assert restored.pending_writes == [
        ("node-a", "value", "earlier"),
        ("node-z", ERROR, "new"),
        ("node-z", "value", "first"),
    ]
    # Exact repeated callback is idempotent; changed payload under same id fails.
    await saver.aput(_thread(task_id), frame, {"step": 0}, {})
    frame["channel_values"] = {"changed": True}
    with pytest.raises(InvalidInvestigationCheckpointError, match="immutable"):
        await saver.aput(_thread(task_id), frame, {"step": 0}, {})
    assert (await saver.aget_tuple(config)).checkpoint == restored.checkpoint
    second = empty_checkpoint()
    second["v"] = format_version
    await saver.aput(config, second, {"step": 1}, {})
    latest = await saver.aget_tuple(_thread(task_id))
    assert latest.parent_config == config
    assert [
        row.metadata["step"]
        async for row in saver.alist(_thread(task_id), before=latest.config, limit=1)
    ] == [0]
    assert [
        row.metadata["step"]
        async for row in saver.alist(_thread(task_id), filter={"step": 1})
    ] == [1]
    async with factory() as session, session.begin():
        await session.execute(
            delete(InvestigationTask).where(InvestigationTask.id == task_id)
        )
    async with factory() as session:
        for model in (
            InvestigationGraphRun,
            InvestigationCheckpoint,
            InvestigationCheckpointWrite,
        ):
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.task_id == task_id)
                )
                == 0
            )


@pytest.mark.parametrize("operation", ["read", "snapshot", "writes", "delete"])
@pytest.mark.parametrize("lost", ["expired", "replaced", "succeeded"])
async def test_postgres_stale_claim_cannot_read_or_modify_progress(
    owned, operation, lost
):
    factory, task_id, _, store, _ = owned
    saver, frame = InvestigationCheckpointSaver(store), empty_checkpoint()
    await saver.aput(_thread(task_id), frame, {}, {})
    async with factory() as session, session.begin():
        changes = (
            {"lease_expires_at": func.clock_timestamp() - timedelta(seconds=1)}
            if lost == "expired"
            else {"claim_token": "replacement"}
            if lost == "replaced"
            else {"status": "succeeded"}
        )
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(**changes)
        )
    with pytest.raises(LeaseLostError):
        if operation == "read":
            await saver.aget_tuple(_thread(task_id))
        elif operation == "snapshot":
            await saver.aput(_thread(task_id, frame["id"]), empty_checkpoint(), {}, {})
        elif operation == "writes":
            await saver.aput_writes(
                _thread(task_id, frame["id"]), [("value", "stale")], "stale"
            )
        else:
            await saver.adelete_thread(task_id)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationCheckpoint)
                .where(InvestigationCheckpoint.task_id == task_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(InvestigationCheckpointWrite)
                .where(InvestigationCheckpointWrite.task_id == task_id)
            )
            == 0
        )


async def test_postgres_frozen_run_is_immutable_and_corruption_is_not_a_fresh_start(
    owned,
):
    factory, task_id, _, store, run = owned
    assert await store.create_run(_input(), _config()) == run
    changed = _input().model_copy(update={"issue_title": "Updated remotely"})
    with pytest.raises(InvalidInvestigationCheckpointError):
        await store.create_run(changed, _config())
    config = _config().model_copy(update={"llm_model": "changed"})
    with pytest.raises(InvalidInvestigationCheckpointError, match="configuration"):
        await load_checkpointed_investigation(store, config)
    async with factory() as session, session.begin():
        await session.execute(
            update(InvestigationGraphRun)
            .where(InvestigationGraphRun.task_id == task_id)
            .values(fingerprint="0" * 64)
        )
    with pytest.raises(InvalidInvestigationCheckpointError):
        await store.load_run()


async def test_postgres_reclaim_resumes_analysis_without_retrieval_or_open_task_transaction(
    owned,
):
    factory, task_id, _, store, run = owned
    history = AsyncMock(return_value=_history())

    async def fail_after_lock_check(request):
        # A separate connection can acquire the task lock during an external await.
        async with asyncio.timeout(5), factory() as session, session.begin():
            assert (
                await session.scalar(
                    select(InvestigationTask.id)
                    .where(InvestigationTask.id == task_id)
                    .with_for_update()
                )
                == task_id
            )
        raise RetryableInvestigationError("model unavailable")

    with pytest.raises(RetryableInvestigationError):
        await execute_checkpointed_investigation(
            run, store, history_searcher=history, analyzer=fail_after_lock_check
        )
    replacement = await _reclaim(factory, task_id)
    assert replacement.claim_token != store.claim_token
    restored = await load_checkpointed_investigation(replacement, _config())
    assert restored == run
    analyzer = AsyncMock(side_effect=_analysis)
    result = await execute_checkpointed_investigation(
        restored, replacement, history_searcher=history, analyzer=analyzer
    )
    assert history.await_count == analyzer.await_count == 1
    assert result.search_round == 1 and result.analysis_evidence
    # Completed final frame can be read by a third owner without any provider call.
    third = await _reclaim(factory, task_id)
    repeated = await execute_checkpointed_investigation(
        run, third, history_searcher=history, analyzer=analyzer
    )
    assert repeated == result and history.await_count == analyzer.await_count == 1


async def test_postgres_result_commit_before_status_crash_recovers_via_existing_runner(
    owned,
):
    factory, task_id, _, _, run = owned
    await _expire(factory, task_id)
    history, analyzer = (
        AsyncMock(return_value=_history()),
        AsyncMock(side_effect=_analysis),
    )

    async def processor(token, *, crash=False):
        store = InvestigationCheckpointStore(
            factory, task_id=task_id, claim_token=token
        )
        result = await execute_checkpointed_investigation(
            run, store, history_searcher=history, analyzer=analyzer
        )
        async with factory() as session, session.begin():
            assert await upsert_investigation_result(
                session, task_id=task_id, claim_token=token, result=result
            )
        if crash:
            raise asyncio.CancelledError("Process died before task success")

    with pytest.raises(asyncio.CancelledError):
        await execute_investigation_task(
            factory, task_id, lambda token: processor(token, crash=True)
        )
    async with factory() as session:
        assert (await session.get(InvestigationTask, task_id)).status == "running"
        assert await session.get(InvestigationResultRecord, task_id) is not None
    await _expire(factory, task_id)
    await execute_investigation_task(factory, task_id, processor)
    async with factory() as session:
        task = await session.get(InvestigationTask, task_id)
        assert task.status == "succeeded" and task.claim_token is None
    assert history.await_count == analyzer.await_count == 1


@pytest.mark.parametrize("replace_owner", [False, True])
async def test_postgres_claim_is_checked_after_real_row_lock_wait(owned, replace_owner):
    factory, task_id, engine, store, _ = owned
    pid_future = asyncio.get_running_loop().create_future()

    class ObservedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if (
                getattr(statement, "_for_update_arg", None) is not None
                and not pid_future.done()
            ):
                pid_future.set_result(await self.scalar(select(func.pg_backend_pid())))
            return await super().execute(statement, *args, **kwargs)

    observed = async_sessionmaker(
        engine, class_=ObservedSession, expire_on_commit=False
    )
    waiting_store = InvestigationCheckpointStore(
        observed, task_id=task_id, claim_token=store.claim_token
    )
    async with factory() as session, session.begin():
        await session.execute(
            update(InvestigationTask)
            .where(InvestigationTask.id == task_id)
            .values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=3))
        )
    operation = None
    try:
        async with factory() as holder, holder.begin():
            await holder.execute(
                select(InvestigationTask.id)
                .where(InvestigationTask.id == task_id)
                .with_for_update()
            )
            operation = asyncio.create_task(waiting_store.load_run())
            pid = await asyncio.wait_for(pid_future, 5)
            async with asyncio.timeout(5):
                while True:
                    async with factory() as session:
                        wait = await session.scalar(
                            text(
                                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                            ),
                            {"pid": pid},
                        )
                    if wait == "Lock":
                        break
                    await asyncio.sleep(0.01)
            assert not operation.done()
            if replace_owner:
                await holder.execute(
                    update(InvestigationTask)
                    .where(InvestigationTask.id == task_id)
                    .values(claim_token="replacement")
                )
            else:
                async with asyncio.timeout(5):
                    while True:
                        async with factory() as session:
                            expired = await session.scalar(
                                select(
                                    InvestigationTask.lease_expires_at
                                    <= func.clock_timestamp()
                                ).where(InvestigationTask.id == task_id)
                            )
                        if expired:
                            break
                        await asyncio.sleep(0.01)
        with pytest.raises(LeaseLostError):
            await asyncio.wait_for(operation, 5)
    finally:
        if operation is not None:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


async def test_postgres_cancellation_rolls_back_progress_and_releases_row_lock(owned):
    factory, task_id, _, store, run = owned
    inserted = asyncio.Event()

    async def uncommitted():
        async with store._transaction() as (session, _):
            session.add(
                InvestigationCheckpoint(
                    task_id=task_id,
                    checkpoint_ns="",
                    checkpoint_id="cancelled",
                    payload_type="bytes",
                    payload=b"x",
                    checkpoint_metadata={},
                )
            )
            await session.flush()
            inserted.set()
            await asyncio.Event().wait()

    operation = asyncio.create_task(uncommitted())
    try:
        await asyncio.wait_for(inserted.wait(), 5)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert await asyncio.wait_for(store.read_checkpoint(""), 5) is None
        assert await store.load_run() == run
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_postgres_expiry_before_commit_rolls_back_checkpoint(owned):
    factory, task_id, _, store, _ = owned

    class ExpiringStore(InvestigationCheckpointStore):
        checks = 0

        async def _assert_claim(self, session):
            self.checks += 1
            if self.checks == 2:
                await session.execute(
                    update(InvestigationTask)
                    .where(InvestigationTask.id == task_id)
                    .values(
                        lease_expires_at=func.clock_timestamp() - timedelta(seconds=1)
                    )
                )
            await super()._assert_claim(session)

    expiring = ExpiringStore(factory, task_id=task_id, claim_token=store.claim_token)
    with pytest.raises(LeaseLostError):
        await InvestigationCheckpointSaver(expiring).aput(
            _thread(task_id), empty_checkpoint(), {}, {}
        )
    assert await store.read_checkpoint("") is None
