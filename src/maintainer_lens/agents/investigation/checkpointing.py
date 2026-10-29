"""Async LangGraph Saver backed by the application's claim-fenced SQL store."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    writes_sort_key,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from maintainer_lens.db.investigation_checkpoints import (
    InvalidInvestigationCheckpointError,
    InvestigationCheckpointStore,
    StoredCheckpoint,
    StoredCheckpointWrite,
)

# The public checkpoint helpers emit v2; the current Graph runtime emits v4.
# Neither is the application's storage or Graph execution version. Keep the
# verified payload formats explicit; package compatibility is checked separately.
_SUPPORTED_CHECKPOINT_FORMAT_VERSIONS = frozenset({2, 4})


def _text(value: Any, name: str, maximum: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or "\0" in value
        or (not empty and not value)
    ):
        raise InvalidInvestigationCheckpointError(f"Invalid checkpoint {name}")
    return value


class InvestigationCheckpointSaver(BaseCheckpointSaver[int]):
    """Persist full snapshots and pending writes for one owned task/thread.

    Only async operations are supported. Administrative cross-task reads,
    pruning, cloning and synchronous Graph execution are intentionally absent.
    Full channel snapshots avoid a separate versioned blob table. The current
    investigation uses ordinary state channels, not DeltaChannel or subgraphs.
    """

    def __init__(self, store: InvestigationCheckpointStore) -> None:
        super().__init__(
            serde=JsonPlusSerializer(
                pickle_fallback=False,
                allowed_json_modules=None,
                allowed_msgpack_modules=None,
            )
        )
        self.store = store
        self._pending: set[asyncio.Task] = set()
        self._aborted = False

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        if self._aborted:
            raise asyncio.CancelledError("Graph checkpoint execution was cancelled")
        task = asyncio.current_task()
        assert task is not None
        self._pending.add(task)
        try:
            yield
        finally:
            self._pending.discard(task)

    def cancel_pending(self, *, exclude: asyncio.Task) -> None:
        """Abort owned Saver I/O before waiting for LangGraph's background exit."""
        self._aborted = True
        for task in tuple(self._pending):
            if task is not exclude:
                task.cancel()

    def _scope(self, config: RunnableConfig) -> tuple[str, str | None]:
        if not isinstance(config, dict):
            raise InvalidInvestigationCheckpointError(
                "Invalid checkpoint configuration"
            )
        configurable = config.get("configurable")
        if (
            not isinstance(configurable, dict)
            or configurable.get("thread_id") != self.store.task_id
        ):
            raise InvalidInvestigationCheckpointError(
                "Checkpoint belongs to another investigation"
            )
        namespace = _text(
            configurable.get("checkpoint_ns", ""), "namespace", 255, empty=True
        )
        checkpoint_id = configurable.get("checkpoint_id")
        if checkpoint_id is not None:
            checkpoint_id = _text(checkpoint_id, "id", 64)
        return namespace, checkpoint_id

    def _config(self, namespace: str, checkpoint_id: str) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": self.store.task_id,
                "checkpoint_ns": namespace,
                "checkpoint_id": checkpoint_id,
            }
        }

    def _load(self, payload_type: str, payload: bytes) -> Any:
        if payload_type not in ("null", "bytes", "bytearray", "json", "msgpack"):
            raise InvalidInvestigationCheckpointError(
                "Unsupported checkpoint serialization"
            )
        try:
            return self.serde.loads_typed((payload_type, payload))
        except (TypeError, ValueError, KeyError, NotImplementedError) as exc:
            raise InvalidInvestigationCheckpointError(
                "Invalid serialized checkpoint"
            ) from exc

    def _checkpoint(self, value: Any, checkpoint_id: str) -> Checkpoint:
        if (
            not isinstance(value, dict)
            or type(value.get("v")) is not int
            or value["v"] not in _SUPPORTED_CHECKPOINT_FORMAT_VERSIONS
            or value.get("id") != checkpoint_id
            or not isinstance(value.get("ts"), str)
            or any(
                not isinstance(value.get(key), dict)
                for key in ("channel_values", "channel_versions", "versions_seen")
            )
        ):
            raise InvalidInvestigationCheckpointError(
                "Invalid LangGraph checkpoint structure"
            )
        return value

    def _tuple(self, record: StoredCheckpoint) -> CheckpointTuple:
        checkpoint = self._checkpoint(
            self._load(record.payload_type, record.payload), record.checkpoint_id
        )
        if not isinstance(record.metadata, dict):
            raise InvalidInvestigationCheckpointError("Invalid checkpoint metadata")
        ordered = sorted(
            record.writes,
            key=lambda row: writes_sort_key(
                row.task_path, row.node_task_id, row.write_index
            ),
        )
        return CheckpointTuple(
            config=self._config(record.namespace, record.checkpoint_id),
            checkpoint=checkpoint,
            metadata=record.metadata,
            parent_config=self._config(record.namespace, record.parent_checkpoint_id)
            if record.parent_checkpoint_id is not None
            else None,
            pending_writes=[
                (
                    row.node_task_id,
                    row.channel,
                    self._load(row.payload_type, row.payload),
                )
                for row in ordered
            ],
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        namespace, checkpoint_id = self._scope(config)
        async with self._operation():
            record = await self.store.read_checkpoint(namespace, checkpoint_id)
        return self._tuple(record) if record is not None else None

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        namespace, parent_id = self._scope(config)
        if not isinstance(checkpoint, dict):
            raise InvalidInvestigationCheckpointError("Invalid checkpoint structure")
        checkpoint_id = _text(checkpoint.get("id"), "id", 64)
        self._checkpoint(checkpoint, checkpoint_id)
        if parent_id == checkpoint_id:
            raise InvalidInvestigationCheckpointError(
                "Checkpoint cannot be its own parent"
            )
        if not isinstance(metadata, dict):
            raise InvalidInvestigationCheckpointError("Invalid checkpoint metadata")
        try:
            metadata = json.loads(json.dumps(metadata, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise InvalidInvestigationCheckpointError(
                "Checkpoint metadata is not JSON"
            ) from exc
        payload_type, payload = self.serde.dumps_typed(checkpoint)
        async with self._operation():
            await self.store.write_checkpoint(
                StoredCheckpoint(
                    namespace, checkpoint_id, parent_id, payload_type, payload, metadata
                )
            )
        return self._config(namespace, checkpoint_id)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        namespace, checkpoint_id = self._scope(config)
        if checkpoint_id is None:
            raise InvalidInvestigationCheckpointError(
                "Node writes require a checkpoint id"
            )
        _text(task_id, "node task id", 128)
        _text(task_path, "task path", 1024, empty=True)
        rows = []
        for index, (channel, value) in enumerate(writes):
            _text(channel, "channel", 255)
            payload_type, payload = self.serde.dumps_typed(value)
            rows.append(
                StoredCheckpointWrite(
                    task_id,
                    WRITES_IDX_MAP.get(channel, index),
                    channel,
                    task_path,
                    payload_type,
                    payload,
                )
            )
        async with self._operation():
            await self.store.write_nodes(namespace, checkpoint_id, rows)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("Checkpoint list limit must be a non-negative integer")
        if filter is not None and not isinstance(filter, dict):
            raise ValueError("Checkpoint metadata filter must be a dictionary")
        namespace, checkpoint_id = (
            self._scope(config) if config is not None else (None, None)
        )
        before_id = self._scope(before)[1] if before is not None else None
        async with self._operation():
            records = await self.store.list_checkpoints(
                namespace=namespace,
                checkpoint_id=checkpoint_id,
                before_id=before_id,
                metadata_filter=filter,
                limit=limit,
            )
        for record in records:
            yield self._tuple(record)

    async def adelete_thread(self, thread_id: str) -> None:
        if thread_id != self.store.task_id:
            raise InvalidInvestigationCheckpointError(
                "Cannot delete another investigation's progress"
            )
        async with self._operation():
            await self.store.delete_checkpoints()
