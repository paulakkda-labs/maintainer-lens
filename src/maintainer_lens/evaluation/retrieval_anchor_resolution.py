"""Resolve manually labeled source evidence to current repository chunk keys."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.repository_index import RepositoryChunk, RepositoryIndex
from maintainer_lens.evaluation.retrieval_dataset import (
    EvidenceAnchor,
    RetrievalDataset,
)
from maintainer_lens.services.repository_vector_search import (
    RepositoryIndexNotFound,
    RepositoryIndexNotReady,
)


class EvidenceAnchorResolutionError(ValueError):
    """A ground-truth anchor cannot be uniquely resolved to one source chunk."""


@dataclass(frozen=True, slots=True)
class _SourceChunk:
    chunk_key: str
    file_path: str
    start_line: int
    end_line: int
    content: str


def _matches_anchor(anchor: EvidenceAnchor, chunk: _SourceChunk) -> bool:
    """Require path, containing line range, and evidence text in that range."""
    if (
        anchor.file_path != chunk.file_path
        or chunk.start_line > anchor.start_line
        or chunk.end_line < anchor.end_line
    ):
        return False

    lines = chunk.content.splitlines(keepends=True)
    expected_lines = chunk.end_line - chunk.start_line + 1

    if len(lines) != expected_lines:
        raise EvidenceAnchorResolutionError(
            f"Inconsistent source line count for chunk {chunk.chunk_key}"
        )

    offset_start = anchor.start_line - chunk.start_line
    offset_end = anchor.end_line - chunk.start_line + 1

    evidence_lines = "".join(lines[offset_start:offset_end])

    return anchor.required_text in evidence_lines


async def resolve_dataset_chunk_keys(
    session: AsyncSession,
    index_id: UUID,
    dataset: RetrievalDataset,
) -> dict[str, frozenset[str]]:
    """Resolve each case's evidence anchors against one ready repository index."""
    paths = sorted(
        {
            anchor.file_path
            for case in dataset.cases
            for anchor in case.relevant_evidence
        }
    )

    async with session.begin():
        index = await session.get(RepositoryIndex, index_id)

        if index is None:
            raise RepositoryIndexNotFound(f"Repository index {index_id} does not exist")

        if index.status != "ready":
            raise RepositoryIndexNotReady(f"Repository index {index_id} is not ready")

        if (
            index.repository != dataset.repository
            or index.commit_sha != dataset.commit_sha
        ):
            raise ValueError(
                "Retrieval dataset does not match repository index revision"
            )

        statement = (
            select(
                RepositoryChunk.chunk_key,
                RepositoryChunk.file_path,
                RepositoryChunk.start_line,
                RepositoryChunk.end_line,
                RepositoryChunk.content,
            )
            .where(
                RepositoryChunk.index_id == index_id,
                RepositoryChunk.file_path.in_(paths),
            )
            .order_by(
                RepositoryChunk.file_path,
                RepositoryChunk.chunk_key,
            )
        )

        rows = (await session.execute(statement)).all()

    chunks = [_SourceChunk(*row) for row in rows]
    resolved: dict[str, frozenset[str]] = {}

    for case in dataset.cases:
        relevant_keys: set[str] = set()

        for anchor in case.relevant_evidence:
            matches = [
                chunk.chunk_key for chunk in chunks if _matches_anchor(anchor, chunk)
            ]

            location = f"{anchor.file_path}:{anchor.start_line}-{anchor.end_line}"

            if not matches:
                raise EvidenceAnchorResolutionError(
                    f"Case {case.id!r}: no chunk matches anchor {location}"
                )

            if len(matches) > 1:
                raise EvidenceAnchorResolutionError(
                    f"Case {case.id!r}: multiple chunks match anchor "
                    f"{location}: {sorted(matches)}"
                )

            relevant_keys.add(matches[0])

        resolved[case.id] = frozenset(relevant_keys)

    return resolved
