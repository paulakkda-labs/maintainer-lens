"""Tests for investigation evidence deduplication."""

from uuid import UUID

from maintainer_lens.agents.investigation.evidence import (
    merge_history_evidence,
    merge_source_evidence,
)
from maintainer_lens.agents.investigation.state import (
    HistoryEvidence,
    SourceEvidence,
)

INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
COMMIT_SHA = "a" * 40


def _history_evidence(number: int) -> HistoryEvidence:
    return {
        "repository": "encode/httpx",
        "number": number,
        "source_type": "item",
        "comment_id": None,
        "title": f"Issue #{number}",
        "content": "Relevant discussion.",
        "source_url": f"https://github.com/encode/httpx/issues/{number}",
        "source_updated_at": "2026-10-25T00:00:00+00:00",
        "distance": 0.12,
    }


def _source_evidence(chunk_key: str) -> SourceEvidence:
    return {
        "index_id": str(INDEX_ID),
        "chunk_key": chunk_key,
        "repository": "encode/httpx",
        "commit_sha": COMMIT_SHA,
        "blob_sha": "b" * 40,
        "file_path": "httpx/_client.py",
        "language": "python",
        "kind": "code",
        "symbol": "send",
        "start_line": 100,
        "end_line": 120,
        "content": f"Source chunk {chunk_key}",
        "source_url": (
            f"https://github.com/encode/httpx/blob/{COMMIT_SHA}/"
            "httpx/_client.py#L100-L120"
        ),
        "rrf_score": 0.03,
        "vector_rank": 1,
        "keyword_rank": 1,
        "cosine_distance": 0.12,
        "matched_in": "content",
    }


def test_history_evidence_merges_and_deduplicates() -> None:
    previous = [
        _history_evidence(3782),
        _history_evidence(1171),
    ]
    current = [
        _history_evidence(3638),
        _history_evidence(3782),
        _history_evidence(3638),
    ]

    merged, new_count = merge_history_evidence(previous, current)

    assert [hit["number"] for hit in merged] == [
        3638,
        3782,
        1171,
    ]
    assert new_count == 1

    assert len(previous) == 2
    assert len(current) == 3


def test_source_evidence_merges_and_deduplicates() -> None:
    previous = [
        _source_evidence("chunk-001"),
    ]
    current = [
        _source_evidence("chunk-002"),
        _source_evidence("chunk-001"),
    ]

    merged, new_count = merge_source_evidence(previous, current)

    assert [hit["chunk_key"] for hit in merged] == [
        "chunk-002",
        "chunk-001",
    ]
    assert new_count == 1


def test_repeated_evidence_does_not_count_as_new() -> None:
    previous = [_history_evidence(3782)]
    current = [_history_evidence(3782)]

    merged, new_count = merge_history_evidence(previous, current)

    assert len(merged) == 1
    assert new_count == 0
