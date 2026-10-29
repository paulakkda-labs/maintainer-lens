"""Claim-fenced persistence for one investigation's Graph execution."""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.investigation_preparation import (
    effective_source_index_expression,
)
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.models.investigation_checkpoint import (
    InvestigationCheckpoint,
    InvestigationCheckpointWrite,
    InvestigationGraphRun,
)
from maintainer_lens.investigation_runner import LeaseLostError
from maintainer_lens.schemas.investigation_checkpoint import (
    CHECKPOINT_STORAGE_VERSION,
    FrozenInvestigationRun,
    GraphCheckpointConfig,
    execution_fingerprint,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationInput


class InvalidInvestigationCheckpointError(ValueError):
    """Stored Graph progress is corrupt, incompatible or outside its task scope."""


@dataclass(frozen=True, slots=True)
class StoredCheckpointWrite:
    node_task_id: str
    write_index: int
    channel: str
    task_path: str
    payload_type: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    namespace: str
    checkpoint_id: str
    parent_checkpoint_id: str | None
    payload_type: str
    payload: bytes
    metadata: dict[str, Any]
    writes: tuple[StoredCheckpointWrite, ...] = ()


def _json_snapshot(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise InvalidInvestigationCheckpointError(
            "Checkpoint metadata is not JSON data"
        ) from exc


def _decode_run(record: InvestigationGraphRun) -> FrozenInvestigationRun:
    if (
        type(record.storage_version) is not int
        or record.storage_version != CHECKPOINT_STORAGE_VERSION
    ):
        raise InvalidInvestigationCheckpointError("Unsupported Graph storage version")
    try:
        return FrozenInvestigationRun.model_validate_json(
            json.dumps(
                {
                    "task_id": record.task_id,
                    "request": record.request_payload,
                    "execution": record.execution_payload,
                    "fingerprint": record.fingerprint,
                },
                allow_nan=False,
            ),
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidInvestigationCheckpointError(
            "Invalid frozen Graph execution"
        ) from exc


class InvestigationCheckpointStore:
    """Bind persistence to one active claim; each call owns a short Session.

    Reads and writes serialize on the existing Task row. Ownership is checked
    after the lock wait and again before commit using the database wall clock.
    No callback holds this transaction while executing a Graph node or HTTP call.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        task_id: str,
        claim_token: str,
    ) -> None:
        if not isinstance(task_id, str) or not 1 <= len(task_id) <= 64:
            raise ValueError("Invalid task_id")
        if not isinstance(claim_token, str) or not claim_token:
            raise ValueError("claim_token must not be blank")
        self.session_factory = session_factory
        self.task_id = task_id
        self.claim_token = claim_token

    async def _assert_claim(self, session: AsyncSession) -> None:
        owned = await session.scalar(
            select(InvestigationTask.id).where(
                InvestigationTask.id == self.task_id,
                InvestigationTask.status == "running",
                InvestigationTask.claim_token == self.claim_token,
                InvestigationTask.lease_expires_at > func.clock_timestamp(),
            )
        )
        if owned is None:
            raise LeaseLostError("Graph persistence requires the current active claim")

    @asynccontextmanager
    async def _transaction(
        self, *, require_run: bool = True
    ) -> AsyncIterator[tuple[AsyncSession, FrozenInvestigationRun | None]]:
        async with self.session_factory() as session, session.begin():
            identity = (
                await session.execute(
                    select(
                        InvestigationTask.repository,
                        InvestigationTask.issue_number,
                        effective_source_index_expression(),
                    )
                    .where(InvestigationTask.id == self.task_id)
                    .with_for_update()
                )
            ).one_or_none()
            await self._assert_claim(session)
            if identity is None:
                raise LeaseLostError("Investigation task is missing")
            record = await session.get(InvestigationGraphRun, self.task_id)
            run = _decode_run(record) if record is not None else None
            if require_run and run is None:
                raise InvalidInvestigationCheckpointError(
                    "Frozen Graph execution is missing"
                )
            if run is not None and tuple(identity) != (
                run.request.repository,
                run.request.issue_number,
                run.request.source_index_id,
            ):
                raise InvalidInvestigationCheckpointError(
                    "Graph execution no longer matches its task"
                )
            yield session, run
            await self._assert_claim(session)

    async def load_run(self) -> FrozenInvestigationRun | None:
        async with self._transaction(require_run=False) as (_, run):
            return run

    async def create_run(
        self, request: InvestigationInput, execution: GraphCheckpointConfig
    ) -> FrozenInvestigationRun:
        # Snapshot before awaits so caller mutation cannot change committed identity.
        try:
            request = InvestigationInput.model_validate(
                request.model_dump(), strict=True
            )
            execution = GraphCheckpointConfig.model_validate(
                execution.model_dump(), strict=True
            )
            run = FrozenInvestigationRun(
                task_id=self.task_id,
                request=request,
                execution=execution,
                fingerprint=execution_fingerprint(request, execution),
            )
            payload = _json_snapshot(run.model_dump(mode="json"))
        except (TypeError, ValueError) as exc:
            raise InvalidInvestigationCheckpointError(
                "Invalid Graph execution input"
            ) from exc
        async with self._transaction(require_run=False) as (session, existing):
            if existing is not None:
                if existing != run:
                    raise InvalidInvestigationCheckpointError(
                        "Cannot replace a frozen Graph execution"
                    )
                return existing
            identity = (
                await session.execute(
                    select(
                        InvestigationTask.repository,
                        InvestigationTask.issue_number,
                        effective_source_index_expression(),
                        InvestigationTask.auto_prepare_knowledge,
                    ).where(InvestigationTask.id == self.task_id)
                )
            ).one()
            if tuple(identity[:3]) != (
                request.repository,
                request.issue_number,
                request.source_index_id,
            ):
                raise InvalidInvestigationCheckpointError(
                    "Graph input does not match its task"
                )
            if identity[3] and request.source_index_id is None:
                raise InvalidInvestigationCheckpointError(
                    "Automatic task has no bound Source index"
                )
            session.add(
                InvestigationGraphRun(
                    task_id=self.task_id,
                    storage_version=CHECKPOINT_STORAGE_VERSION,
                    request_payload=payload["request"],
                    execution_payload=payload["execution"],
                    fingerprint=run.fingerprint,
                )
            )
            await session.flush()
            return run

    async def _checkpoint(
        self, session: AsyncSession, record: InvestigationCheckpoint
    ) -> StoredCheckpoint:
        writes = (
            await session.scalars(
                select(InvestigationCheckpointWrite).where(
                    InvestigationCheckpointWrite.task_id == self.task_id,
                    InvestigationCheckpointWrite.checkpoint_ns == record.checkpoint_ns,
                    InvestigationCheckpointWrite.checkpoint_id == record.checkpoint_id,
                )
            )
        ).all()
        return StoredCheckpoint(
            namespace=record.checkpoint_ns,
            checkpoint_id=record.checkpoint_id,
            parent_checkpoint_id=record.parent_checkpoint_id,
            payload_type=record.payload_type,
            payload=bytes(record.payload),
            metadata=_json_snapshot(record.checkpoint_metadata),
            writes=tuple(
                StoredCheckpointWrite(
                    node_task_id=row.node_task_id,
                    write_index=row.write_index,
                    channel=row.channel,
                    task_path=row.task_path,
                    payload_type=row.payload_type,
                    payload=bytes(row.payload),
                )
                for row in writes
            ),
        )

    async def read_checkpoint(
        self, namespace: str, checkpoint_id: str | None = None
    ) -> StoredCheckpoint | None:
        async with self._transaction() as (session, _):
            statement = select(InvestigationCheckpoint).where(
                InvestigationCheckpoint.task_id == self.task_id,
                InvestigationCheckpoint.checkpoint_ns == namespace,
            )
            if checkpoint_id is not None:
                statement = statement.where(
                    InvestigationCheckpoint.checkpoint_id == checkpoint_id
                )
            record = await session.scalar(
                statement.order_by(InvestigationCheckpoint.checkpoint_id.desc()).limit(
                    1
                )
            )
            return (
                await self._checkpoint(session, record) if record is not None else None
            )

    async def write_checkpoint(self, checkpoint: StoredCheckpoint) -> None:
        metadata = _json_snapshot(checkpoint.metadata)
        async with self._transaction() as (session, _):
            statement = (
                insert(InvestigationCheckpoint)
                .values(
                    task_id=self.task_id,
                    checkpoint_ns=checkpoint.namespace,
                    checkpoint_id=checkpoint.checkpoint_id,
                    parent_checkpoint_id=checkpoint.parent_checkpoint_id,
                    payload_type=checkpoint.payload_type,
                    payload=checkpoint.payload,
                    checkpoint_metadata=metadata,
                )
                .on_conflict_do_nothing(
                    index_elements=["task_id", "checkpoint_ns", "checkpoint_id"]
                )
            )
            await session.execute(statement)
            stored = await session.get(
                InvestigationCheckpoint,
                (self.task_id, checkpoint.namespace, checkpoint.checkpoint_id),
            )
            if (
                stored.parent_checkpoint_id,
                stored.payload_type,
                bytes(stored.payload),
                stored.checkpoint_metadata,
            ) != (
                checkpoint.parent_checkpoint_id,
                checkpoint.payload_type,
                checkpoint.payload,
                metadata,
            ):
                raise InvalidInvestigationCheckpointError(
                    "Cannot overwrite an immutable checkpoint"
                )

    async def write_nodes(
        self,
        namespace: str,
        checkpoint_id: str,
        writes: Sequence[StoredCheckpointWrite],
    ) -> None:
        # Freeze the sequence before acquiring any database resource.
        writes = tuple(writes)
        async with self._transaction() as (session, _):
            for row in writes:
                statement = insert(InvestigationCheckpointWrite).values(
                    task_id=self.task_id,
                    checkpoint_ns=namespace,
                    checkpoint_id=checkpoint_id,
                    node_task_id=row.node_task_id,
                    write_index=row.write_index,
                    channel=row.channel,
                    task_path=row.task_path,
                    payload_type=row.payload_type,
                    payload=row.payload,
                )
                keys = [
                    "task_id",
                    "checkpoint_ns",
                    "checkpoint_id",
                    "node_task_id",
                    "write_index",
                ]
                if row.write_index < 0:
                    statement = statement.on_conflict_do_update(
                        index_elements=keys,
                        set_={
                            "channel": statement.excluded.channel,
                            "task_path": statement.excluded.task_path,
                            "payload_type": statement.excluded.payload_type,
                            "payload": statement.excluded.payload,
                        },
                    )
                else:
                    statement = statement.on_conflict_do_nothing(index_elements=keys)
                await session.execute(statement)

    async def list_checkpoints(
        self,
        *,
        namespace: str | None,
        checkpoint_id: str | None,
        before_id: str | None,
        metadata_filter: dict[str, Any] | None,
        limit: int | None,
    ) -> list[StoredCheckpoint]:
        async with self._transaction() as (session, _):
            statement = select(InvestigationCheckpoint).where(
                InvestigationCheckpoint.task_id == self.task_id
            )
            if namespace is not None:
                statement = statement.where(
                    InvestigationCheckpoint.checkpoint_ns == namespace
                )
            if checkpoint_id is not None:
                statement = statement.where(
                    InvestigationCheckpoint.checkpoint_id == checkpoint_id
                )
            if before_id is not None:
                statement = statement.where(
                    InvestigationCheckpoint.checkpoint_id < before_id
                )
            records = (
                await session.scalars(
                    statement.order_by(InvestigationCheckpoint.checkpoint_id.desc())
                )
            ).all()
            selected = [
                row
                for row in records
                if not metadata_filter
                or all(
                    row.checkpoint_metadata.get(key) == value
                    for key, value in metadata_filter.items()
                )
            ]
            if limit is not None:
                selected = selected[:limit]
            return [await self._checkpoint(session, row) for row in selected]

    async def delete_checkpoints(self) -> None:
        async with self._transaction() as (session, _):
            await session.execute(
                delete(InvestigationCheckpointWrite).where(
                    InvestigationCheckpointWrite.task_id == self.task_id
                )
            )
            await session.execute(
                delete(InvestigationCheckpoint).where(
                    InvestigationCheckpoint.task_id == self.task_id
                )
            )
