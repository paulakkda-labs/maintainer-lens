"""Tests for GitHub investigation error classification."""

from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

from maintainer_lens.investigation_runner import (
    PermanentInvestigationError,
    RetryableInvestigationError,
)
from maintainer_lens.services.issue_investigator import (
    GitHubRateLimitError,
    investigate_issue,
)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status_code", "headers", "body", "expected_error"),
    [
        (404, {}, "", PermanentInvestigationError),
        (401, {}, "", PermanentInvestigationError),
        (403, {}, "", PermanentInvestigationError),
        (500, {}, "", RetryableInvestigationError),
        (503, {}, "", RetryableInvestigationError),
        (
            403,
            {"x-ratelimit-remaining": "0"},
            "",
            GitHubRateLimitError,
        ),
        (
            403,
            {},
            "You have exceeded a secondary rate limit.",
            GitHubRateLimitError,
        ),
        (
            429,
            {"retry-after": "120"},
            "",
            GitHubRateLimitError,
        ),
    ],
)
async def test_github_http_error_classification(
    status_code: int,
    headers: dict[str, str],
    body: str,
    expected_error: type[Exception],
) -> None:
    """Classify GitHub HTTP errors before opening a database session."""

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status_code,
            headers=headers,
            text=body,
        )
    )

    session_factory = Mock()

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(expected_error):
            await investigate_issue(
                http_client=client,
                session_factory=session_factory,
                task_id="task-001",
                claim_token="token-001",
                repository="encode/httpx",
                issue_number=3712,
            )

    session_factory.assert_not_called()


@pytest.mark.anyio
async def test_network_error_is_retryable() -> None:
    """Classify network failures as retryable investigation errors."""

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "Connection failed",
            request=request,
        )

    session_factory = Mock()

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(RetryableInvestigationError):
            await investigate_issue(
                http_client=client,
                session_factory=session_factory,
                task_id="task-001",
                claim_token="token-001",
                repository="encode/httpx",
                issue_number=3712,
            )

    session_factory.assert_not_called()


@pytest.mark.anyio
async def test_rate_limit_preserves_retry_deadline() -> None:
    """Preserve the earliest permitted retry time."""

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            429,
            headers={"retry-after": "120"},
        )
    )

    session_factory = Mock()
    before = datetime.now(UTC)

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(GitHubRateLimitError) as exc_info:
            await investigate_issue(
                http_client=client,
                session_factory=session_factory,
                task_id="task-001",
                claim_token="token-001",
                repository="encode/httpx",
                issue_number=3712,
            )

    assert (exc_info.value.not_before - before).total_seconds() >= 120

    session_factory.assert_not_called()
