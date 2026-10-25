"""Embed and persist selected GitHub history records."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.models.repository_history_details import (
    RepositoryHistoryCommentRecord,
)
from maintainer_lens.db.models.repository_history_embedding import (
    RepositoryHistoryEmbedding,
)

MAX_CONTENT_CHARS = 5000


@dataclass(frozen=True, slots=True)
class HistoryIndexingResult:
    """Number of indexed and reused evidence records."""

    documents_found: int
    documents_embedded: int
    documents_reused: int


@dataclass(frozen=True, slots=True)
class _HistoryDocument:
    """One historical source prepared for embedding."""

    source_key: str
    source_type: str
    number: int
    comment_id: int | None
    title: str
    content: str
    source_url: str
    source_updated_at: datetime
    embedding_text: str
    content_sha256: str


def _validate_vector(
    vector: list[float],
    dimensions: int,
) -> None:
    if (
        len(vector) != dimensions
        or not all(math.isfinite(value) for value in vector)
        or not any(value != 0 for value in vector)
    ):
        raise ValueError("Invalid history embedding vector")


async def index_repository_history(
    session_factory: async_sessionmaker[AsyncSession],
    embedding_client: EmbeddingProvider,
    repository: str,
    numbers: Sequence[int],
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    max_comments: int = 30,
    batch_size: int = 10,
) -> HistoryIndexingResult:
    """Index selected history records without re-embedding unchanged sources."""

    if not repository.strip():
        raise ValueError("Repository must not be blank")
    if not embedding_provider_name.strip() or not embedding_model.strip():
        raise ValueError("Embedding configuration cannot be blank")
    if type(embedding_dimensions) is not int or embedding_dimensions <= 0:
        raise ValueError("embedding_dimensions must be positive")
    if type(batch_size) is not int or not 1 <= batch_size <= 20:
        raise ValueError("batch_size must be between 1 and 20")
    if type(max_comments) is not int or not 0 <= max_comments <= 100:
        raise ValueError("max_comments must be between 0 and 100")

    selected_numbers = tuple(numbers)

    if (
        not 1 <= len(selected_numbers) <= 10
        or any(type(number) is not int or number <= 0 for number in selected_numbers)
        or len(set(selected_numbers)) != len(selected_numbers)
    ):
        raise ValueError("Provide 1 to 10 distinct positive issue numbers")

    history = RepositoryHistoryRecord
    comment = RepositoryHistoryCommentRecord
    stored = RepositoryHistoryEmbedding

    # Load source records without holding a transaction during embedding calls.
    async with session_factory() as session:
        parents = (
            await session.scalars(
                select(history)
                .where(
                    history.repository == repository,
                    history.number.in_(selected_numbers),
                )
                .order_by(history.number)
            )
        ).all()

        if len(parents) != len(selected_numbers):
            raise ValueError("Some history records are missing from PostgreSQL")

        comments = (
            await session.scalars(
                select(comment)
                .where(
                    comment.repository == repository,
                    comment.issue_number.in_(selected_numbers),
                )
                .order_by(
                    comment.source_updated_at.desc(),
                    comment.comment_id.desc(),
                )
                .limit(max_comments)
            )
        ).all()

        documents: list[_HistoryDocument] = []
        parents_by_number = {item.number: item for item in parents}

        for item in parents:
            content = (item.body or "")[:MAX_CONTENT_CHARS]
            embedding_text = f"{item.kind} #{item.number}: {item.title}\n{content}"

            documents.append(
                _HistoryDocument(
                    source_key=f"item:{item.number}",
                    source_type="item",
                    number=item.number,
                    comment_id=None,
                    title=item.title,
                    content=content,
                    source_url=item.source_url,
                    source_updated_at=item.source_updated_at,
                    embedding_text=embedding_text,
                    content_sha256=sha256(embedding_text.encode("utf-8")).hexdigest(),
                )
            )

        for entry in comments:
            parent = parents_by_number[entry.issue_number]
            content = (entry.body or "")[:MAX_CONTENT_CHARS]

            if not content.strip():
                continue

            embedding_text = (
                f"Discussion on {parent.kind} #{parent.number}: "
                f"{parent.title}\n{content}"
            )

            documents.append(
                _HistoryDocument(
                    source_key=f"comment:{entry.comment_id}",
                    source_type="comment",
                    number=entry.issue_number,
                    comment_id=entry.comment_id,
                    title=parent.title,
                    content=content,
                    source_url=entry.source_url,
                    source_updated_at=entry.source_updated_at,
                    embedding_text=embedding_text,
                    content_sha256=sha256(embedding_text.encode("utf-8")).hexdigest(),
                )
            )

        existing_rows = await session.execute(
            select(
                stored.source_key,
                stored.content_sha256,
                stored.source_updated_at,
            ).where(
                stored.repository == repository,
                stored.embedding_provider == embedding_provider_name,
                stored.embedding_model == embedding_model,
                stored.embedding_dimensions == embedding_dimensions,
                stored.source_key.in_([document.source_key for document in documents]),
            )
        )

        existing = {
            row.source_key: (
                row.content_sha256,
                row.source_updated_at,
            )
            for row in existing_rows
        }

    pending = [
        document
        for document in documents
        if existing.get(document.source_key)
        != (document.content_sha256, document.source_updated_at)
    ]

    if not pending:
        return HistoryIndexingResult(
            documents_found=len(documents),
            documents_embedded=0,
            documents_reused=len(documents),
        )

    rows: list[dict] = []

    for offset in range(0, len(pending), batch_size):
        batch = pending[offset : offset + batch_size]

        vectors = await embedding_client.embed_documents(
            [document.embedding_text for document in batch]
        )

        if len(vectors) != len(batch):
            raise ValueError("History embedding count mismatch")

        for document, vector in zip(batch, vectors, strict=True):
            _validate_vector(vector, embedding_dimensions)

            rows.append(
                {
                    "repository": repository,
                    "source_key": document.source_key,
                    "embedding_provider": embedding_provider_name,
                    "embedding_model": embedding_model,
                    "embedding_dimensions": embedding_dimensions,
                    "source_type": document.source_type,
                    "number": document.number,
                    "comment_id": document.comment_id,
                    "title": document.title,
                    "content": document.content,
                    "source_url": document.source_url,
                    "source_updated_at": document.source_updated_at,
                    "content_sha256": document.content_sha256,
                    "embedding": vector,
                }
            )

    statement = insert(stored).values(rows)

    statement = statement.on_conflict_do_update(
        index_elements=[
            stored.repository,
            stored.source_key,
            stored.embedding_provider,
            stored.embedding_model,
            stored.embedding_dimensions,
        ],
        set_={
            "title": statement.excluded.title,
            "content": statement.excluded.content,
            "source_url": statement.excluded.source_url,
            "source_updated_at": statement.excluded.source_updated_at,
            "content_sha256": statement.excluded.content_sha256,
            "embedding": statement.excluded.embedding,
        },
        where=(statement.excluded.source_updated_at >= stored.source_updated_at),
    )

    async with session_factory() as session:
        async with session.begin():
            await session.execute(statement)

    return HistoryIndexingResult(
        documents_found=len(documents),
        documents_embedded=len(pending),
        documents_reused=len(documents) - len(pending),
    )
