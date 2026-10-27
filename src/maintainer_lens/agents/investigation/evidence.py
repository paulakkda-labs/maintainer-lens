"""Evidence conversion, provenance checks, and deduplication."""

from string import hexdigits
from urllib.parse import quote, urlsplit
from uuid import UUID

from maintainer_lens.agents.investigation.state import (
    HistoryEvidence,
    SourceEvidence,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)
from maintainer_lens.services.repository_hybrid_search import (
    HybridSearchResult,
)


def build_source_url(hit: HybridSearchResult) -> str:
    """Build a GitHub source link pinned to the indexed commit."""

    if len(hit.commit_sha) != 40 or any(
        character not in hexdigits for character in hit.commit_sha
    ):
        raise ValueError("Source evidence has an invalid commit SHA")

    if (
        not hit.file_path
        or hit.file_path.startswith("/")
        or ".." in hit.file_path.split("/")
    ):
        raise ValueError("Source evidence has an invalid file path")

    if hit.start_line < 1 or hit.end_line < hit.start_line:
        raise ValueError("Source evidence has an invalid line range")

    encoded_path = quote(hit.file_path, safe="/")

    if hit.start_line == hit.end_line:
        line_fragment = f"L{hit.start_line}"
    else:
        line_fragment = f"L{hit.start_line}-L{hit.end_line}"

    return (
        f"https://github.com/{hit.repository}/blob/"
        f"{hit.commit_sha}/{encoded_path}#{line_fragment}"
    )


def to_history_evidence(
    hit: RepositoryHistorySemanticHit,
    repository: str,
) -> HistoryEvidence:
    """Validate and convert a history search hit to graph evidence."""

    if hit.repository != repository:
        raise ValueError("History evidence belongs to another repository")

    if hit.source_updated_at.utcoffset() is None:
        raise ValueError("History evidence timestamp must include a timezone")

    record: HistoryEvidence = {
        "repository": hit.repository,
        "number": hit.number,
        "source_type": hit.source_type,
        "comment_id": hit.comment_id,
        "title": hit.title,
        "content": hit.content,
        "source_url": hit.source_url,
        "source_updated_at": hit.source_updated_at.isoformat(),
        "distance": hit.distance,
    }
    history_evidence_id(record)
    return record


def history_evidence_id(hit: HistoryEvidence) -> str:
    """Validate historical provenance before assigning a citation ID."""
    number = hit["number"]
    if type(number) is not int or number <= 0:
        raise ValueError("History evidence has an invalid issue number")

    url = urlsplit(hit["source_url"])
    repository = hit["repository"].casefold()
    paths = {
        f"/{repository}/issues/{number}",
        f"/{repository}/pull/{number}",
    }
    if (
        url.scheme != "https"
        or url.netloc.casefold() != "github.com"
        or url.path.casefold() not in paths
        or url.query
    ):
        raise ValueError("History evidence has an invalid source URL")

    if hit["source_type"] == "comment":
        comment_id = hit["comment_id"]
        if type(comment_id) is not int or comment_id <= 0:
            raise ValueError("Historical comment evidence is missing its ID")
        if url.fragment != f"issuecomment-{comment_id}":
            raise ValueError("History comment URL does not match its ID")
        return f"history:comment:{comment_id}"
    if hit["source_type"] != "item" or hit["comment_id"] is not None or url.fragment:
        raise ValueError("History evidence has an invalid object identity")
    return f"history:item:{number}"


def to_source_evidence(
    hit: HybridSearchResult,
    repository: str,
    index_id: UUID,
) -> SourceEvidence:
    """Validate and convert a source search hit to graph evidence."""

    if hit.index_id != index_id:
        raise ValueError("Source evidence belongs to another index")

    if hit.repository != repository:
        raise ValueError("Source evidence belongs to another repository")

    return {
        "index_id": str(hit.index_id),
        "chunk_key": hit.chunk_key,
        "repository": hit.repository,
        "commit_sha": hit.commit_sha,
        "blob_sha": hit.blob_sha,
        "file_path": hit.file_path,
        "language": hit.language,
        "kind": hit.kind,
        "symbol": hit.symbol,
        "start_line": hit.start_line,
        "end_line": hit.end_line,
        "content": hit.content,
        "source_url": build_source_url(hit),
        "rrf_score": hit.rrf_score,
        "vector_rank": hit.vector_rank,
        "keyword_rank": hit.keyword_rank,
        "cosine_distance": hit.cosine_distance,
        "matched_in": hit.matched_in,
    }


def merge_history_evidence(
    previous: list[HistoryEvidence],
    current: list[HistoryEvidence],
) -> tuple[list[HistoryEvidence], int]:
    """Keep current hits first and deduplicate historical evidence."""

    def identity(hit: HistoryEvidence) -> tuple[str, str, int, int | None]:
        return hit["repository"], hit["source_type"], hit["number"], hit["comment_id"]

    previous_ids = {identity(hit) for hit in previous}
    seen: set[tuple[str, str, int, int | None]] = set()
    merged: list[HistoryEvidence] = []
    new_count = 0

    for hit in [*current, *previous]:
        key = identity(hit)

        if key in seen:
            continue

        seen.add(key)
        merged.append(hit)

        if key not in previous_ids:
            new_count += 1

    return merged, new_count


def merge_source_evidence(
    previous: list[SourceEvidence],
    current: list[SourceEvidence],
) -> tuple[list[SourceEvidence], int]:
    """Keep current hits first and deduplicate source evidence."""

    def identity(hit: SourceEvidence) -> tuple[str, str]:
        return hit["index_id"], hit["chunk_key"]

    previous_ids = {identity(hit) for hit in previous}
    seen: set[tuple[str, str]] = set()
    merged: list[SourceEvidence] = []
    new_count = 0

    for hit in [*current, *previous]:
        key = identity(hit)

        if key in seen:
            continue

        seen.add(key)
        merged.append(hit)

        if key not in previous_ids:
            new_count += 1

    return merged, new_count
