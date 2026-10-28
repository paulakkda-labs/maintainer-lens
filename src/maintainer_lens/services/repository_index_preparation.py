"""Spool complete Source indexes while enforcing persisted preparation budgets."""

import json
from collections.abc import Iterator
from itertools import batched
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.clients.embedding import EmbeddingProvider
from maintainer_lens.clients.github_blob import fetch_repository_blob
from maintainer_lens.investigation_runner import PermanentInvestigationError
from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.investigation_budget import KnowledgePreparationBudget
from maintainer_lens.schemas.repository import (
    RepositoryManifest,
    RepositoryRevision,
    RepositorySource,
)
from maintainer_lens.services.repository_index_writer import (
    EmbeddedChunk,
    write_repository_index,
)
from maintainer_lens.services.source_chunker import chunk_repository_source


class KnowledgePreparationBudgetExceeded(PermanentInvestigationError):
    """The selected finite preparation budget cannot cover this repository."""


def _check_limit(name: str, actual: int, maximum: int | None) -> None:
    if maximum is not None and actual > maximum:
        raise KnowledgePreparationBudgetExceeded(f"Knowledge budget exceeded: {name}")


async def prepare_source_index_contents(
    github_client: httpx.AsyncClient,
    embedding_client: EmbeddingProvider,
    session: AsyncSession,
    revision: RepositoryRevision,
    manifest: RepositoryManifest,
    *,
    budget: KnowledgePreparationBudget,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    chunker_version: str,
    embedding_batch_size: int,
    owner_token: UUID,
) -> UUID:
    """Validate all selected chunks before embeddings, then publish atomically.

    A temporary directory bounds resident source/vector content by one file and
    one embedding batch. It is removed on success, failure and cancellation.
    Index identity/leases and manifest selection policy belong to the builder.
    """
    if (manifest.repository, manifest.commit_sha, manifest.tree_sha) != (
        revision.repository,
        revision.commit_sha,
        revision.tree_sha,
    ):
        raise ValueError("Source manifest does not match its pinned revision")
    _check_limit("source files", len(manifest.files), budget.source_max_files)
    _check_limit(
        "source bytes",
        sum(file.size for file in manifest.files),
        budget.source_max_bytes,
    )
    with TemporaryDirectory(prefix="maintainer-lens-source-") as directory:
        chunk_path = Path(directory) / "chunks.jsonl"
        embedded_path = Path(directory) / "embedded.jsonl"
        chunk_count = 0
        with chunk_path.open("w", encoding="utf-8") as output:
            for file in manifest.files:
                content = await fetch_repository_blob(
                    github_client, revision.repository, file
                )
                chunks = chunk_repository_source(
                    RepositorySource(file=file, content=content)
                )
                chunk_count += len(chunks)
                _check_limit("source chunks", chunk_count, budget.source_max_chunks)
                for chunk in chunks:
                    output.write(chunk.model_dump_json() + "\n")
        if chunk_count == 0:
            raise ValueError("No indexable source chunks found")

        with (
            chunk_path.open(encoding="utf-8") as source,
            embedded_path.open("w", encoding="utf-8") as output,
        ):
            for lines in batched(source, embedding_batch_size):
                chunks = [CodeChunk.model_validate_json(line) for line in lines]
                vectors = await embedding_client.embed_documents(
                    [chunk.content for chunk in chunks]
                )
                if len(vectors) != len(chunks):
                    raise ValueError("Embedding batch result count mismatch")
                for chunk, vector in zip(chunks, vectors, strict=True):
                    output.write(
                        json.dumps(
                            {"chunk": chunk.model_dump(), "vector": vector},
                            allow_nan=False,
                        )
                        + "\n"
                    )

        def replay() -> Iterator[EmbeddedChunk]:
            with embedded_path.open(encoding="utf-8") as source:
                for line in source:
                    item = json.loads(line)
                    yield EmbeddedChunk(
                        CodeChunk.model_validate(item["chunk"]), item["vector"]
                    )

        return await write_repository_index(
            session,
            revision,
            replay,
            embedding_provider=embedding_provider_name,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            chunker_version=chunker_version,
            owner_token=owner_token,
        )
