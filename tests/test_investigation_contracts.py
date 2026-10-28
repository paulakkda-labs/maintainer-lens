"""Regression tests for shared investigation models and legacy imports."""

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError

from maintainer_lens.schemas.investigation_api import (
    CreateInvestigationRequest as ApiCreateInvestigationRequest,
)
from maintainer_lens.schemas.investigation_contracts import (
    CreateInvestigationRequest,
    InvestigationInput,
    InvestigationResult,
)
from maintainer_lens.services.investigation_execution import (
    InvestigationInput as ApplicationInvestigationInput,
)
from maintainer_lens.services.investigation_execution import (
    InvestigationResult as ApplicationInvestigationResult,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")
API_MODULE = "maintainer_lens.schemas.investigation_api"
CONTRACT_MODULE = "maintainer_lens.schemas.investigation_contracts"
EXECUTION_MODULE = "maintainer_lens.services.investigation_execution"


def _input_payload() -> dict:
    return {
        "repository": "encode/httpx",
        "issue_number": 9000,
        "issue_title": "Connection cancellation",
        "issue_body": None,
        "response_language": "en",
        "source_index_id": str(INDEX_ID),
    }


def _result_payload() -> dict:
    source_url = "https://github.com/encode/httpx/issues/3782"
    return {
        **_input_payload(),
        "next_step": "draft_preliminary_report",
        "preliminary_analysis": {
            "summary": "A relevant historical discussion was retrieved.",
            "findings": [
                {
                    "statement": "The discussion describes cancellation.",
                    "evidence_ids": ["history:item:3782"],
                }
            ],
            "hypotheses": [],
            "unanswered_questions": [],
        },
        "history_evidence": [
            {
                "repository": "encode/httpx",
                "number": 3782,
                "source_type": "item",
                "comment_id": None,
                "title": "Cancellation discussion",
                "content": "Cancellation affects connection cleanup.",
                "source_url": source_url,
                "source_updated_at": "2026-10-25T00:00:00+00:00",
                "distance": 0.12,
            }
        ],
        "source_evidence": [],
        "analysis_evidence": [
            {
                "evidence_id": "history:item:3782",
                "category": "history",
                "source_url": source_url,
                "content": (
                    "Cancellation discussion\nCancellation affects connection cleanup."
                ),
            }
        ],
        "search_round": 1,
        "routing_reason": "Cited findings have no declared gaps.",
    }


def _run_fresh_python(script: str) -> None:
    """Check cold imports without the current pytest process's module cache."""
    completed = subprocess.run(
        [sys.executable, "-c", dedent(script)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_legacy_imports_reexport_the_same_model_classes() -> None:
    assert ApiCreateInvestigationRequest is CreateInvestigationRequest
    assert ApplicationInvestigationInput is InvestigationInput
    assert ApplicationInvestigationResult is InvestigationResult
    assert InvestigationInput.__bases__ == (CreateInvestigationRequest,)
    assert InvestigationResult.__bases__ == (InvestigationInput,)


def test_http_request_retains_existing_coercion_and_extra_field_policy() -> None:
    request = ApiCreateInvestigationRequest.model_validate(
        {"repository": "encode/httpx", "issue_number": "12", "unused": "ignored"}
    )

    assert request.model_dump() == {"repository": "encode/httpx", "issue_number": 12}


@pytest.mark.parametrize("model", [CreateInvestigationRequest, InvestigationInput])
def test_repository_validation_is_shared_by_http_and_application(model) -> None:
    with pytest.raises(ValidationError, match="owner/repository"):
        model.model_validate({**_input_payload(), "repository": "owner/../repo"})


@pytest.mark.parametrize(
    "changes",
    [
        {"issue_number": "12"},
        {"issue_number": True},
        {"history_evidence": []},
    ],
)
def test_application_input_retains_strictness_and_state_injection_rejection(changes):
    with pytest.raises(ValidationError):
        InvestigationInput.model_validate({**_input_payload(), **changes})


def test_application_input_retains_uuid_conversion_title_and_frozen_fields() -> None:
    request = InvestigationInput.model_validate(
        {**_input_payload(), "issue_title": "  Connection cancellation  "}
    )

    assert request.source_index_id == INDEX_ID
    assert request.issue_title == "  Connection cancellation  "
    assert request.issue_body is None
    with pytest.raises(ValidationError, match="frozen_instance"):
        request.issue_title = "Changed"


def test_result_retains_json_round_trip_and_can_be_used_in_a_response_schema() -> None:
    class ResultEnvelope(BaseModel):
        task_id: str
        result: InvestigationResult

    payload = _result_payload()
    result = InvestigationResult.model_validate(payload)
    encoded = result.model_dump_json()

    assert json.loads(encoded) == payload
    assert InvestigationResult.model_validate_json(encoded) == result
    assert result.preliminary_analysis.findings[0].evidence_ids == ["history:item:3782"]
    assert result.analysis_evidence == payload["analysis_evidence"]

    response = ResultEnvelope(task_id="task-001", result=result)
    assert ResultEnvelope.model_validate_json(response.model_dump_json()) == response
    assert ResultEnvelope.model_json_schema()["properties"]["result"]["$ref"] == (
        "#/$defs/InvestigationResult"
    )


def test_shared_contract_import_does_not_load_api_execution_or_graph_composition():
    _run_fresh_python(
        f"""
        import importlib
        import sys
        from unittest.mock import patch

        with patch(
            "maintainer_lens.core.config.get_settings",
            side_effect=AssertionError("Model imports must not load settings"),
        ):
            importlib.import_module({CONTRACT_MODULE!r})

        for forbidden in (
            {API_MODULE!r},
            {EXECUTION_MODULE!r},
            "maintainer_lens.agents.investigation_graph",
        ):
            assert forbidden not in sys.modules, forbidden
        """
    )


@pytest.mark.parametrize(
    "order",
    [
        (API_MODULE, EXECUTION_MODULE, CONTRACT_MODULE),
        (EXECUTION_MODULE, API_MODULE, CONTRACT_MODULE),
        (CONTRACT_MODULE, API_MODULE, EXECUTION_MODULE),
    ],
    ids=["api_first", "execution_first", "contracts_first"],
)
def test_public_models_import_in_fresh_process_regardless_of_import_order(order):
    _run_fresh_python(
        f"""
        import importlib
        from unittest.mock import patch

        with patch(
            "maintainer_lens.core.config.get_settings",
            side_effect=AssertionError("Model imports must not load settings"),
        ):
            for name in {order!r}:
                importlib.import_module(name)

        contracts = importlib.import_module({CONTRACT_MODULE!r})
        api = importlib.import_module({API_MODULE!r})
        execution = importlib.import_module({EXECUTION_MODULE!r})
        assert api.CreateInvestigationRequest is contracts.CreateInvestigationRequest
        assert execution.InvestigationInput is contracts.InvestigationInput
        assert execution.InvestigationResult is contracts.InvestigationResult
        """
    )
