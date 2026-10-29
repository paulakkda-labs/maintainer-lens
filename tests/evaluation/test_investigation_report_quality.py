"""Offline review-set and saved-context observations; no providers or database."""

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from maintainer_lens.evaluation.report_quality import (
    ReportReviewDataset,
    inspect_report_evidence,
    load_report_review_dataset,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationResult

DATASET = Path(__file__).resolve().parents[1] / "fixtures/reports/sampleproject.json"
INDEX = UUID("3b111111-1111-4111-8111-111111111111")
REPOSITORY = "review-tests/repo"


@pytest.fixture
def result():
    history = [
        {
            "repository": REPOSITORY,
            "number": number,
            "source_type": "item",
            "comment_id": None,
            "title": "Prior discussion",
            "content": "An example statement.",
            "source_url": f"https://github.com/{REPOSITORY}/issues/{number}",
            "source_updated_at": "2026-10-29T00:00:00+00:00",
            "distance": 0.1,
        }
        for number in (124, 125)
    ]
    source_url = f"https://github.com/{REPOSITORY}/blob/{'a' * 40}/sample.py#L1"
    source_id = f"source:{INDEX}:chunk-1"
    return InvestigationResult(
        repository=REPOSITORY,
        issue_number=123,
        issue_title="Review example",
        issue_body=None,
        response_language="en",
        source_index_id=INDEX,
        next_step="needs_more_evidence",
        search_round=2,
        routing_reason="Human accepted an earlier complete analysis.",
        preliminary_analysis={
            "summary": "A complete earlier analysis.",
            "findings": [
                {
                    "statement": "A cited observation.",
                    "evidence_ids": ["history:item:124", source_id],
                },
                {
                    "statement": "Another observation.",
                    "evidence_ids": ["history:item:124"],
                },
            ],
            "hypotheses": ["A hypothesis still needs verification."],
            "unanswered_questions": ["Which version is affected?"],
        },
        history_evidence=history,
        source_evidence=[
            {
                "index_id": str(INDEX),
                "chunk_key": "chunk-1",
                "repository": REPOSITORY,
                "commit_sha": "a" * 40,
                "blob_sha": "b" * 40,
                "file_path": "sample.py",
                "language": "python",
                "kind": "file",
                "symbol": None,
                "start_line": 1,
                "end_line": 1,
                "content": "answer = 42\n",
                "source_url": source_url,
                "rrf_score": 0.03,
                "vector_rank": 1,
                "keyword_rank": None,
                "cosine_distance": 0.1,
                "matched_in": None,
            }
        ],
        analysis_evidence=[
            {
                "evidence_id": "history:item:124",
                "category": "history",
                "source_url": history[0]["source_url"],
                "content": "Earlier actual excerpt.",
            },
            {
                "evidence_id": source_id,
                "category": "source",
                "source_url": source_url,
                "content": "answer = 42\n",
            },
        ],
    )


def test_observations_distinguish_retrieved_analyzed_and_distinct_cited_evidence(
    result,
):
    observations = inspect_report_evidence(result)
    assert observations.retrieved_history == 2 and observations.analyzed_history == 1
    assert observations.retrieved_source == observations.analyzed_source == 1
    assert observations.finding_count == observations.distinct_cited_evidence == 2
    assert observations.cited_history == observations.cited_source == 1
    assert observations.hypothesis_count == observations.unanswered_question_count == 1
    assert observations.next_step == "needs_more_evidence"
    assert observations.search_round == 2
    assert not any("accuracy" in key or "score" in key for key in asdict(observations))


@pytest.mark.parametrize(
    "corruption", ["foreign-url", "unanalyzed-citation", "false-ready"]
)
def test_observations_reuse_provenance_and_readiness_checks(result, corruption):
    payload = result.model_dump()
    if corruption == "foreign-url":
        payload["history_evidence"][0]["source_url"] = "https://example.com/124"
    elif corruption == "unanalyzed-citation":
        payload["preliminary_analysis"]["findings"][0]["evidence_ids"] = [
            "history:item:125"
        ]
    else:
        payload["next_step"] = "draft_preliminary_report"
    with pytest.raises(ValueError):
        inspect_report_evidence(InvestigationResult.model_validate(payload))


def test_empty_evidence_is_a_valid_insufficient_report(result):
    payload = result.model_dump()
    payload.update(history_evidence=[], source_evidence=[], analysis_evidence=[])
    payload["preliminary_analysis"]["findings"] = []
    observations = inspect_report_evidence(InvestigationResult.model_validate(payload))
    assert observations.finding_count == observations.distinct_cited_evidence == 0
    assert observations.analyzed_history == observations.analyzed_source == 0
    assert observations.next_step == "needs_more_evidence"


def test_review_samples_are_small_public_prompts_not_fixed_expected_model_answers():
    dataset = load_report_review_dataset(DATASET)
    assert {case.issue_number for case in dataset.cases} == {200, 221, 225}
    assert {case.repository for case in dataset.cases} == {"pypa/sampleproject"}
    assert all(case.source_index_id is None for case in dataset.cases)
    assert all(
        case.issue_url.endswith(f"/issues/{case.issue_number}")
        for case in dataset.cases
    )
    assert {case.observed_issue_state for case in dataset.cases} == {"open", "closed"}


@pytest.mark.parametrize(
    "corruption", ["duplicate-id", "duplicate-issue", "blank-focus", "source-uuid"]
)
def test_review_set_rejects_ambiguous_or_unreviewable_cases(corruption):
    payload = deepcopy(load_report_review_dataset(DATASET).model_dump())
    if corruption == "duplicate-id":
        payload["cases"][1]["id"] = payload["cases"][0]["id"]
    elif corruption == "duplicate-issue":
        payload["cases"][1]["issue_number"] = payload["cases"][0]["issue_number"]
    elif corruption == "blank-focus":
        payload["cases"][0]["review_focus"] = [" "]
    else:
        payload["cases"][0]["source_index_id"] = INDEX
    with pytest.raises(ValidationError):
        ReportReviewDataset.model_validate(payload)
