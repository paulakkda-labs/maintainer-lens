"""Validate an explicitly selected Source index for an investigation."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.schemas.investigation_contracts import InvestigationIdentity
from maintainer_lens.services.repository_vector_search import RepositoryIndexNotReady


@dataclass(frozen=True, slots=True)
class InvestigationSourceIndex:
    """Validated index identity detached from the caller's database Session."""

    index_id: UUID
    repository: str
    commit_sha: str
    tree_sha: str
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    chunker_version: str


class InvestigationSourceIndexNotReadyError(RepositoryIndexNotReady):
    """An explicitly requested index is building or failed, rather than ready."""

    def __init__(self, index_id: UUID, status: str) -> None:
        self.index_id = index_id
        self.status = status
        super().__init__(f"Source index {index_id} is not ready: status={status}")


async def get_investigation_source_index(
    session: AsyncSession,
    *,
    repository: str,
    source_index_id: UUID,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> InvestigationSourceIndex:
    """Read one selected ready index; the caller owns the read transaction.

    Validate local inputs before the first query. Do not select a latest index,
    resolve remote HEAD, build knowledge, lock rows, commit or implement retries.
    This snapshot is not a lease: existing retrievers must keep their checks.
    """
    if not isinstance(repository, str):
        raise ValueError("Repository must be a string")
    InvestigationIdentity.validate_repository(repository)
    if not isinstance(source_index_id, UUID):
        raise ValueError("source_index_id must be a UUID")
    for name, value in (
        ("embedding_provider_name", embedding_provider_name),
        ("embedding_model", embedding_model),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be blank")
    if type(embedding_dimensions) is not int or embedding_dimensions <= 0:
        raise ValueError("embedding_dimensions must be a positive integer")

    index = await session.get(RepositoryIndex, source_index_id, populate_existing=True)
    if index is None:
        raise ValueError("Source index does not exist")
    if index.repository != repository:
        raise ValueError("Source index does not belong to the requested repository")
    if index.status != "ready":
        raise InvestigationSourceIndexNotReadyError(source_index_id, index.status)
    for name, expected in (
        ("embedding_provider", embedding_provider_name),
        ("embedding_model", embedding_model),
        ("embedding_dimensions", embedding_dimensions),
    ):
        if getattr(index, name) != expected:
            raise ValueError(f"Source index {name} does not match query configuration")

    return InvestigationSourceIndex(
        index_id=index.id,
        repository=index.repository,
        commit_sha=index.commit_sha,
        tree_sha=index.tree_sha,
        embedding_provider=index.embedding_provider,
        embedding_model=index.embedding_model,
        embedding_dimensions=index.embedding_dimensions,
        chunker_version=index.chunker_version,
    )
