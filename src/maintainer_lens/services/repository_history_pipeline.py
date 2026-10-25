"""Prepare a bounded repository history knowledge batch for retrieval."""

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.services.repository_history_collection import (
    HistoryStopReason,
    collect_repository_history,
)
from maintainer_lens.services.repository_history_enrichment import (
    collect_repository_history_enrichment,
    save_repository_history_enrichment,
)
from maintainer_lens.services.repository_history_indexing import (
    index_repository_history,
)
from maintainer_lens.services.repository_history_persistence import (
    save_repository_history_batch,
)


@dataclass(frozen=True, slots=True)
class RepositoryHistoryPreparationResult:
    """Summary of a bounded history knowledge preparation run."""

    repository: str
    items_collected: int
    history_stop_reason: HistoryStopReason
    comments_collected: int
    pull_requests_collected: int
    incomplete_comment_numbers: tuple[int, ...]
    unprocessed_enrichment_items: int
    documents_found: int
    documents_embedded: int
    documents_reused: int


async def prepare_repository_history(
    session_factory: async_sessionmaker[AsyncSession],
    github_client: httpx.AsyncClient,
    embedding_client: EmbeddingProvider,
    repository: str,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    max_items: int = 3,
    detail_items: int = 2,
    comments_per_page: int = 5,
    embedding_batch_size: int = 5,
) -> RepositoryHistoryPreparationResult:
    """Prepare a small, searchable history batch without advancing sync cursors."""

    if not repository.strip():
        raise ValueError("Repository must not be blank")

    if type(max_items) is not int or not 1 <= max_items <= 5:
        raise ValueError("max_items must be between 1 and 5")

    if type(detail_items) is not int or not 1 <= detail_items <= 5:
        raise ValueError("detail_items must be between 1 and 5")

    if type(comments_per_page) is not int or not 1 <= comments_per_page <= 10:
        raise ValueError("comments_per_page must be between 1 and 10")

    if type(embedding_batch_size) is not int or not 1 <= embedding_batch_size <= 20:
        raise ValueError("embedding_batch_size must be between 1 and 20")

    if not embedding_provider_name.strip() or not embedding_model.strip():
        raise ValueError("Embedding configuration cannot be blank")

    if type(embedding_dimensions) is not int or embedding_dimensions <= 0:
        raise ValueError("embedding_dimensions must be positive")

    # Collect only one bounded GitHub history page.
    batch = await collect_repository_history(
        github_client,
        repository,
        per_page=max_items,
        max_pages=1,
        max_items=max_items,
    )

    if not batch.items:
        return RepositoryHistoryPreparationResult(
            repository=repository,
            items_collected=0,
            history_stop_reason=batch.stop_reason,
            comments_collected=0,
            pull_requests_collected=0,
            incomplete_comment_numbers=(),
            unprocessed_enrichment_items=0,
            documents_found=0,
            documents_embedded=0,
            documents_reused=0,
        )

    # Persist parent records before inserting dependent details.
    async with session_factory() as session:
        await save_repository_history_batch(
            session,
            repository,
            batch,
        )

    enrichment = await collect_repository_history_enrichment(
        github_client,
        repository,
        batch.items,
        max_items=detail_items,
        max_comment_pages=1,
        comments_per_page=comments_per_page,
    )

    async with session_factory() as session:
        await save_repository_history_enrichment(
            session,
            enrichment,
        )

    # Index all collected parent records and a bounded set of comments.
    indexing = await index_repository_history(
        session_factory,
        embedding_client,
        repository,
        [item.number for item in batch.items],
        embedding_provider_name=embedding_provider_name,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        max_comments=10,
        batch_size=embedding_batch_size,
    )

    return RepositoryHistoryPreparationResult(
        repository=repository,
        items_collected=len(batch.items),
        history_stop_reason=batch.stop_reason,
        comments_collected=len(enrichment.comments),
        pull_requests_collected=len(enrichment.pull_requests),
        incomplete_comment_numbers=enrichment.incomplete_comment_numbers,
        unprocessed_enrichment_items=enrichment.unprocessed_items,
        documents_found=indexing.documents_found,
        documents_embedded=indexing.documents_embedded,
        documents_reused=indexing.documents_reused,
    )
