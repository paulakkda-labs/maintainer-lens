"""Persist complete repository vector indexes atomically."""

import asyncio
import hashlib
import json
import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from itertools import batched
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.repository_index import (
    RepositoryChunk,
    RepositoryIndex,
)
from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import RepositoryRevision
from maintainer_lens.services.repository_index_lease import (
    assert_repository_index_lease_owned,
    make_repository_index_identity_key,
)


@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    """A source chunk paired with its generated embedding."""

    chunk: CodeChunk
    vector: list[float]


class RepositoryIndexConflict(Exception):
    """An existing index cannot safely be reused."""


def make_chunk_key(chunk: CodeChunk) -> str:
    """Compute a deterministic key from source and chunk metadata."""

    payload = {
        "file_path": chunk.file.path,
        "blob_sha": chunk.file.blob_sha,
        "language": chunk.language,
        "kind": chunk.kind,
        "symbol": chunk.symbol,
        "symbols": chunk.symbols,
        "heading_path": chunk.heading_path,
        "headings": chunk.headings,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "content": chunk.content,
    }

    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prepare_items(
    items: Sequence[EmbeddedChunk],
    dimensions: int,
) -> list[tuple[EmbeddedChunk, str]]:
    """Validate vectors and prepare unique chunk identities."""

    if dimensions <= 0:
        raise ValueError("Embedding dimensions must be positive")

    if not items:
        raise ValueError("Repository index requires at least one chunk")

    prepared: list[tuple[EmbeddedChunk, str]] = []
    seen_keys: set[str] = set()

    for item in items:
        if len(item.vector) != dimensions:
            raise ValueError("Embedding dimension mismatch")

        if not all(math.isfinite(value) for value in item.vector):
            raise ValueError("Embedding contains non-finite values")

        key = make_chunk_key(item.chunk)

        if key in seen_keys:
            raise ValueError("Duplicate chunk key in input")

        seen_keys.add(key)
        prepared.append((item, key))

    return prepared


async def write_repository_index(
    session: AsyncSession,
    revision: RepositoryRevision,
    items: Sequence[EmbeddedChunk] | Callable[[], Iterator[EmbeddedChunk]],
    *,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
    chunker_version: str,
    owner_token: UUID | None = None,
) -> UUID:
    """Atomically store a repository index with optional lease fencing.

    A callable supplies a stable, replayable local spool. Validation precedes
    SQL; persistence replays bounded batches without retaining all vectors.
    The callable must never perform network I/O or mutate between replays.
    """

    if any(
        not value.strip()
        for value in (
            embedding_provider,
            embedding_model,
            chunker_version,
        )
    ):
        raise ValueError("Index configuration fields cannot be empty")

    if callable(items):

        def replay():
            seen: set[str] = set()
            for item in items():
                pair = _prepare_items([item], embedding_dimensions)[0]
                if pair[1] in seen:
                    raise ValueError("Duplicate chunk key in input")
                seen.add(pair[1])
                yield pair
            if not seen:
                raise ValueError("Repository index requires at least one chunk")

        expected_keys: set[str] = set()
        for batch in batched(replay(), 50):
            expected_keys.update(key for _, key in batch)
            # Let deadlines, cancellation and independent lease renewal run.
            await asyncio.sleep(0)
    else:
        prepared = _prepare_items(items, embedding_dimensions)
        expected_keys = {key for _, key in prepared}

        def replay():
            yield from prepared

    async with session.begin():
        if owner_token is not None:
            identity_key = make_repository_index_identity_key(
                revision,
                embedding_provider_name=embedding_provider,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                chunker_version=chunker_version,
            )

            await assert_repository_index_lease_owned(
                session,
                identity_key=identity_key,
                owner_token=owner_token,
            )

        statement = (
            insert(RepositoryIndex)
            .values(
                id=uuid4(),
                repository=revision.repository,
                commit_sha=revision.commit_sha,
                tree_sha=revision.tree_sha,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                chunker_version=chunker_version,
                status="building",
            )
            .on_conflict_do_nothing(constraint="uq_repository_indexes_identity")
            .returning(RepositoryIndex.id)
        )

        index_id = (await session.execute(statement)).scalar_one_or_none()

        if index_id is None:
            existing_statement = select(RepositoryIndex).where(
                RepositoryIndex.repository == revision.repository,
                RepositoryIndex.commit_sha == revision.commit_sha,
                RepositoryIndex.embedding_provider == embedding_provider,
                RepositoryIndex.embedding_model == embedding_model,
                RepositoryIndex.embedding_dimensions == embedding_dimensions,
                RepositoryIndex.chunker_version == chunker_version,
            )

            existing = (await session.scalars(existing_statement)).one()

            if existing.status != "ready":
                raise RepositoryIndexConflict("Existing repository index is not ready")

            if existing.tree_sha != revision.tree_sha:
                raise RepositoryIndexConflict(
                    "Existing repository index has a different tree SHA"
                )

            stored_statement = select(RepositoryChunk.chunk_key).where(
                RepositoryChunk.index_id == existing.id
            )

            stored_keys = set((await session.scalars(stored_statement)).all())

            if stored_keys != expected_keys:
                raise RepositoryIndexConflict(
                    "Existing repository index has different chunks"
                )

            return existing.id

        written_keys: set[str] = set()
        for batch in batched(replay(), 50):
            records = [
                RepositoryChunk(
                    index_id=index_id,
                    chunk_key=key,
                    file_path=item.chunk.file.path,
                    blob_sha=item.chunk.file.blob_sha,
                    language=item.chunk.language,
                    kind=item.chunk.kind,
                    symbol=item.chunk.symbol,
                    symbols=item.chunk.symbols,
                    heading_path=item.chunk.heading_path,
                    headings=item.chunk.headings,
                    start_line=item.chunk.start_line,
                    end_line=item.chunk.end_line,
                    content=item.chunk.content,
                    embedding=item.vector,
                )
                for item, key in batch
            ]

            session.add_all(records)
            await session.flush()
            written_keys.update(key for _, key in batch)
            # The Session holds weak references after flush; release this batch.
            del records

        if written_keys != expected_keys:
            raise ValueError("Repository index spool changed during persistence")

        if owner_token is not None:
            await assert_repository_index_lease_owned(
                session, identity_key=identity_key, owner_token=owner_token
            )

        await session.execute(
            update(RepositoryIndex)
            .where(RepositoryIndex.id == index_id)
            .values(status="ready")
        )

    return index_id
