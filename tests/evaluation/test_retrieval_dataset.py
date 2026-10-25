import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from maintainer_lens.evaluation.retrieval_dataset import (
    RetrievalDataset,
    load_retrieval_dataset,
)
from maintainer_lens.services.repository_query_analysis import (
    analyze_repository_query,
)

DATASET_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "retrieval"
    / "sampleproject.json"
)

EXPECTED_KEYWORDS = {
    "add-one-en": None,
    "add-one-zh": None,
    "entry-point-en": None,
    "entry-point-zh": None,
    "add-one-identifier-en": "add_one",
    "add-one-identifier-zh": "add_one",
    "entry-point-identifier-en": "main",
    "entry-point-identifier-zh": "main",
}


def load_payload() -> dict[str, Any]:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def test_load_retrieval_dataset() -> None:
    dataset = load_retrieval_dataset(DATASET_PATH)

    assert dataset.schema_version == 1
    assert dataset.repository == "pypa/sampleproject"
    assert dataset.commit_sha == ("621e4974ca25ce531773def586ba3ed8e736b3fc")
    assert len(dataset.cases) == 8

    cases = {case.id: case for case in dataset.cases}

    assert set(cases) == set(EXPECTED_KEYWORDS)

    assert sum(case.language == "en" for case in dataset.cases) == 4
    assert sum(case.language == "zh" for case in dataset.cases) == 4

    for prefix in ("add-one", "entry-point"):
        expected = cases[f"{prefix}-en"].relevant_evidence

        assert cases[f"{prefix}-zh"].relevant_evidence == expected
        assert cases[f"{prefix}-identifier-en"].relevant_evidence == expected
        assert cases[f"{prefix}-identifier-zh"].relevant_evidence == expected


def test_query_analysis_matches_dataset_expectations() -> None:
    dataset = load_retrieval_dataset(DATASET_PATH)

    extracted = {}

    for case in dataset.cases:
        analysis = analyze_repository_query(case.query)

        assert analysis.query == case.query
        extracted[case.id] = analysis.keyword

    assert extracted == EXPECTED_KEYWORDS


def test_dataset_generates_json_schema() -> None:
    schema = RetrievalDataset.model_json_schema()

    assert schema["type"] == "object"
    assert "cases" in schema["properties"]
    assert "repository" in schema["properties"]
    assert "commit_sha" in schema["properties"]


def test_rejects_duplicate_case_ids() -> None:
    payload = load_payload()
    payload["cases"][1]["id"] = payload["cases"][0]["id"]

    with pytest.raises(ValidationError, match="case ids must be unique"):
        RetrievalDataset.model_validate(payload)


@pytest.mark.parametrize(
    "file_path",
    [
        "../src/sample/simple.py",
        "/tmp/simple.py",
        "src\\sample\\simple.py",
        "src//sample/simple.py",
    ],
)
def test_rejects_invalid_file_path(file_path: str) -> None:
    payload = load_payload()
    payload["cases"][0]["relevant_evidence"][0]["file_path"] = file_path

    with pytest.raises(ValidationError, match="file_path"):
        RetrievalDataset.model_validate(payload)


def test_rejects_invalid_line_range() -> None:
    payload = load_payload()
    anchor = payload["cases"][0]["relevant_evidence"][0]

    anchor["start_line"] = 10
    anchor["end_line"] = 2

    with pytest.raises(ValidationError, match="start_line"):
        RetrievalDataset.model_validate(payload)


def test_rejects_missing_evidence() -> None:
    payload = load_payload()
    payload["cases"][0]["relevant_evidence"] = []

    with pytest.raises(ValidationError):
        RetrievalDataset.model_validate(payload)


def test_rejects_duplicate_evidence() -> None:
    payload = load_payload()
    evidence = payload["cases"][0]["relevant_evidence"]
    evidence.append(evidence[0].copy())

    with pytest.raises(ValidationError, match="duplicate anchors"):
        RetrievalDataset.model_validate(payload)


def test_rejects_blank_query() -> None:
    payload = load_payload()
    payload["cases"][0]["query"] = "   "

    with pytest.raises(ValidationError, match="query must not be blank"):
        RetrievalDataset.model_validate(payload)


def test_rejects_invalid_commit_sha() -> None:
    payload = load_payload()
    payload["commit_sha"] = "not-a-valid-sha"

    with pytest.raises(ValidationError):
        RetrievalDataset.model_validate(payload)


def test_rejects_unknown_fields() -> None:
    payload = load_payload()
    payload["unexpected_field"] = True

    with pytest.raises(ValidationError, match="Extra inputs"):
        RetrievalDataset.model_validate(payload)
