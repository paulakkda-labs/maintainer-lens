"""Prepare repository history knowledge in bounded pages for retrieval."""

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.clients.github_history import fetch_repository_history_page
from maintainer_lens.services.repository_history_collection import (
    HistoryStopReason,
    RepositoryHistoryBatch,
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

    return await _prepare_history_batch(
        session_factory,
        github_client,
        embedding_client,
        repository,
        batch,
        embedding_provider_name=embedding_provider_name,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        detail_items=detail_items,
        comments_per_page=comments_per_page,
        embedding_batch_size=embedding_batch_size,
    )


async def _prepare_history_batch(
    session_factory: async_sessionmaker[AsyncSession],
    github_client: httpx.AsyncClient,
    embedding_client: EmbeddingProvider,
    repository: str,
    batch: RepositoryHistoryBatch,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    detail_items: int,
    comments_per_page: int,
    embedding_batch_size: int,
) -> RepositoryHistoryPreparationResult:
    """Persist, enrich and index one bounded page without a network transaction."""
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


async def prepare_repository_history_pages(
    session_factory: async_sessionmaker[AsyncSession],
    github_client: httpx.AsyncClient,
    embedding_client: EmbeddingProvider,
    repository: str,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    max_items: int | None,
) -> int:
    """Prepare recent parent records in pages; None removes the parent-item cap.

    Each page retains the established detail policy: at most two parents are
    enriched, with one page of five comments each; indexing uses up to ten
    stored comments per parent. This is retrieval preparation, not full sync.
    Existing content-identical History embeddings are reused by the indexer.
    """
    if max_items is not None and (type(max_items) is not int or max_items <= 0):
        raise ValueError("History item budget must be positive or None")
    if (
        not repository.strip()
        or not embedding_provider_name.strip()
        or not embedding_model.strip()
    ):
        raise ValueError("History preparation configuration cannot be blank")
    if type(embedding_dimensions) is not int or embedding_dimensions <= 0:
        raise ValueError("Embedding dimensions must be positive")
    page_number = 1
    seen: set[int] = set()
    total = 0
    per_page = min(max_items, 5) if max_items is not None else 5
    while True:
        page = await fetch_repository_history_page(
            github_client, repository, page=page_number, per_page=per_page
        )
        items = []
        for item in page.items:
            if item.repository != repository:
                raise ValueError("History item belongs to another repository")
            if item.number not in seen and (
                max_items is None or total + len(items) < max_items
            ):
                seen.add(item.number)
                items.append(item)
        if items:
            await _prepare_history_batch(
                session_factory,
                github_client,
                embedding_client,
                repository,
                RepositoryHistoryBatch(
                    tuple(items),
                    1,
                    "exhausted" if page.next_page is None else "item_limit",
                ),
                embedding_provider_name=embedding_provider_name,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                detail_items=2,
                comments_per_page=5,
                embedding_batch_size=5,
            )
            total += len(items)
        if page.next_page is None or (max_items is not None and total >= max_items):
            return total
        if page.next_page <= page_number or not page.items:
            raise ValueError("Invalid history pagination progress")
        page_number = page.next_page
