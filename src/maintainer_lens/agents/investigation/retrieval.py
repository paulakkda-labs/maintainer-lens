"""Repository retrieval adapters and LangGraph search nodes."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.agents.investigation.evidence import (
    merge_history_evidence,
    merge_source_evidence,
    to_history_evidence,
    to_source_evidence,
)
from maintainer_lens.agents.investigation.state import (
    HistoryEvidence,
    HistorySearcher,
    InvestigationState,
    SourceEvidence,
    SourceSearcher,
)
from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.db.models.repository_index import RepositoryIndex
from maintainer_lens.services.repository_history_semantic_search import (
    search_repository_history_semantic,
)
from maintainer_lens.services.repository_search import search_repository


def create_history_searcher(
    session_factory: async_sessionmaker[AsyncSession],
    embedding_client: EmbeddingProvider,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    top_k: int = 5,
) -> HistorySearcher:
    """Bind database and embedding dependencies to history search."""

    async def search(
        repository: str,
        query: str,
    ):
        async with session_factory() as session:
            return await search_repository_history_semantic(
                session,
                embedding_client,
                repository,
                query,
                embedding_provider_name=embedding_provider_name,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
                top_k=top_k,
            )

    return search


def create_source_searcher(
    session_factory: async_sessionmaker[AsyncSession],
    embedding_client: EmbeddingProvider,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    candidate_limit: int = 10,
    top_k: int = 5,
    rank_constant: int = 60,
) -> SourceSearcher:
    """Bind source retrieval and validate the requested index."""

    async def search(
        repository: str,
        index_id: UUID,
        query: str,
    ):
        async with session_factory() as session:
            repository_index = await session.get(
                RepositoryIndex,
                index_id,
            )

            if repository_index is None:
                raise ValueError("Source index does not exist")

            if repository_index.repository != repository:
                raise ValueError(
                    "Source index does not belong to the requested repository"
                )

            return await search_repository(
                session,
                embedding_client,
                index_id,
                query,
                embedding_provider_name=embedding_provider_name,
                embedding_model=embedding_model,
                candidate_limit=candidate_limit,
                top_k=top_k,
                rank_constant=rank_constant,
            )

    return search


def make_history_search_node(history_searcher: HistorySearcher):
    """Create the LangGraph node for cumulative history retrieval."""

    async def search_history(state: InvestigationState) -> dict:
        hits = await history_searcher(
            state["repository"],
            state["history_query"],
        )

        current: list[HistoryEvidence] = [
            to_history_evidence(hit, state["repository"]) for hit in hits
        ]

        merged, new_count = merge_history_evidence(
            state.get("history_evidence", []),
            current,
        )

        return {
            "history_evidence": merged,
            "new_history_evidence": new_count,
        }

    return search_history


def make_source_search_node(source_searcher: SourceSearcher):
    """Create the LangGraph node for cumulative source retrieval."""

    async def search_source(state: InvestigationState) -> dict:
        source_index_id = UUID(state["source_index_id"])

        hits = await source_searcher(
            state["repository"],
            source_index_id,
            state["history_query"],
        )

        current: list[SourceEvidence] = [
            to_source_evidence(
                hit,
                state["repository"],
                source_index_id,
            )
            for hit in hits
        ]

        merged, new_count = merge_source_evidence(
            state.get("source_evidence", []),
            current,
        )

        return {
            "source_evidence": merged,
            "new_source_evidence": new_count,
        }

    return search_source
