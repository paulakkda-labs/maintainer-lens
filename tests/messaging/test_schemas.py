"""Unit tests for investigation message parsing and validation."""

import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from maintainer_lens.messaging.schemas import (
    InvestigationRequestedMessage,
    parse_investigation_message,
)


def test_parse_valid_investigation_message() -> None:
    """Parse a valid investigation request into a validated model."""
    payload = {
        "task_id": "task-001",
        "repository": "fastapi/fastapi",
        "issue_number": 123,
    }

    message = Mock(body=json.dumps(payload).encode("utf-8"))

    result = parse_investigation_message(message)

    assert isinstance(result, InvestigationRequestedMessage)
    assert result.task_id == "task-001"
    assert result.repository == "fastapi/fastapi"
    assert result.issue_number == 123


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"repository": "fastapi/fastapi", "issue_number": 123},
            id="missing-task-id",
        ),
        pytest.param(
            {"task_id": "", "repository": "fastapi/fastapi", "issue_number": 123},
            id="empty-task-id",
        ),
        pytest.param(
            {"task_id": "task-001", "repository": "", "issue_number": 123},
            id="empty-repository",
        ),
        pytest.param(
            {"task_id": "task-001", "repository": "fastapi/fastapi", "issue_number": 0},
            id="zero-issue-number",
        ),
        pytest.param(
            {
                "task_id": "task-001",
                "repository": "fastapi/fastapi",
                "issue_number": -1,
            },
            id="negative-issue-number",
        ),
        pytest.param(
            {
                "task_id": "task-001",
                "repository": "fastapi/fastapi",
                "issue_number": "123",
            },
            id="string-issue-number",
        ),
        pytest.param(
            {
                "task_id": "task-001",
                "repository": "fastapi/fastapi",
                "issue_number": True,
            },
            id="boolean-issue-number",
        ),
        pytest.param(
            {
                "task_id": "task-001",
                "repository": "fastapi/fastapi",
                "issue_number": 123,
                "unexpected": "value",
            },
            id="unexpected-field",
        ),
    ],
)
def test_parse_invalid_investigation_message(payload: dict) -> None:
    """Reject investigation requests with invalid fields."""
    message = Mock(body=json.dumps(payload).encode("utf-8"))

    with pytest.raises(ValidationError):
        parse_investigation_message(message)


def test_parse_malformed_json_message() -> None:
    """Reject messages containing malformed JSON."""
    message = Mock(body=b'{"task_id":')

    with pytest.raises(ValidationError):
        parse_investigation_message(message)
