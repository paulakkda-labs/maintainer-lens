"""Prepare reusable knowledge without owning task claims or Graph execution."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.schemas.investigation_budget import KnowledgePreparationBudget
from maintainer_lens.services.issue_investigator import (
    GitHubRateLimitError,
    get_github_retry_time,
    is_github_rate_limited,
)
from maintainer_lens.services.repository_history_pipeline import (
    prepare_repository_history_pages,
)
from maintainer_lens.services.repository_indexing import build_repository_index

AUTO_SOURCE_CHUNKER_VERSION = "investigation-auto-v1"
type KnowledgePreparer = Callable[[str, KnowledgePreparationBudget], Awaitable[UUID]]


class KnowledgePreparationDeadlineExceeded(TimeoutError):
    """The task's own knowledge-preparation deadline expired."""


def create_knowledge_preparer(
    session_factory: async_sessionmaker[AsyncSession],
    github_client: httpx.AsyncClient,
    embedding_client: EmbeddingProvider,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> KnowledgePreparer:
    """Bind existing Worker resources; returned calls open only scoped Sessions."""
    if not embedding_provider_name.strip() or not embedding_model.strip():
        raise ValueError("Embedding configuration cannot be blank")
    if type(embedding_dimensions) is not int or embedding_dimensions <= 0:
        raise ValueError("Embedding dimensions must be positive")

    async def prepare(repository: str, budget: KnowledgePreparationBudget) -> UUID:
        # Revalidate plain data, including caller-supplied constructed models.
        budget = KnowledgePreparationBudget.model_validate(budget.model_dump())
        timeout_scope = asyncio.timeout(budget.preparation_timeout_seconds)
        try:
            async with timeout_scope:
                async with session_factory() as session:
                    index_id = await build_repository_index(
                        github_client,
                        embedding_client,
                        session,
                        repository,
                        lease_session_factory=session_factory,
                        embedding_provider_name=embedding_provider_name,
                        embedding_model=embedding_model,
                        embedding_dimensions=embedding_dimensions,
                        chunker_version=AUTO_SOURCE_CHUNKER_VERSION,
                        preparation_budget=budget,
                    )
                if timeout_scope.expired():
                    raise KnowledgePreparationDeadlineExceeded(
                        "Knowledge preparation deadline exceeded"
                    )
                await prepare_repository_history_pages(
                    session_factory,
                    github_client,
                    embedding_client,
                    repository,
                    embedding_provider_name=embedding_provider_name,
                    embedding_model=embedding_model,
                    embedding_dimensions=embedding_dimensions,
                    max_items=budget.history_max_items,
                )
        except TimeoutError as exc:
            if timeout_scope.expired():
                raise KnowledgePreparationDeadlineExceeded(
                    "Knowledge preparation deadline exceeded"
                ) from exc
            raise
        except httpx.HTTPStatusError as exc:
            if exc.request.url.host == "api.github.com" and is_github_rate_limited(
                exc.response
            ):
                raise GitHubRateLimitError(get_github_retry_time(exc.response)) from exc
            raise
        # A dependency may catch cancellation and return; it still cannot succeed.
        if timeout_scope.expired():
            raise KnowledgePreparationDeadlineExceeded(
                "Knowledge preparation deadline exceeded"
            )
        return index_id

    return prepare
