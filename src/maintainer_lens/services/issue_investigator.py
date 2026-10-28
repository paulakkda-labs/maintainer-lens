"""GitHub issue investigation business logic."""

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.github import fetch_github_issue
from maintainer_lens.db.issue_snapshots import (
    IssueSnapshotData,
    upsert_issue_snapshot,
)
from maintainer_lens.investigation_runner import (
    LeaseLostError,
    PermanentInvestigationError,
    RetryableInvestigationError,
)


class GitHubRateLimitError(RetryableInvestigationError):
    """A GitHub rate limit error with a retry deadline."""

    def __init__(self, not_before: datetime) -> None:
        self.not_before = not_before
        super().__init__("GitHub API rate limit exceeded")


def is_github_rate_limited(response: httpx.Response) -> bool:
    """Identify GitHub primary and secondary rate limits."""

    if response.status_code == 429:
        return True

    if response.status_code != 403:
        return False

    if response.headers.get("x-ratelimit-remaining") == "0":
        return True

    if "retry-after" in response.headers:
        return True

    return "rate limit" in response.text.lower()


def get_github_retry_time(response: httpx.Response) -> datetime:
    """Calculate the earliest retry time from GitHub response headers."""

    now = datetime.now(UTC)
    not_before = now + timedelta(minutes=1)

    retry_after = response.headers.get("retry-after")

    if retry_after is not None:
        try:
            seconds = max(0, int(retry_after))
            not_before = max(
                not_before,
                now + timedelta(seconds=seconds),
            )
        except (ValueError, OverflowError):
            pass

    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset")

        if reset is not None:
            try:
                reset_time = datetime.fromtimestamp(
                    int(reset),
                    tz=UTC,
                )
                not_before = max(
                    not_before,
                    reset_time + timedelta(seconds=1),
                )
            except (ValueError, OverflowError):
                pass

    return not_before


async def investigate_issue(
    *,
    http_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
    claim_token: str,
    repository: str,
    issue_number: int,
) -> IssueSnapshotData:
    """Fetch an issue and return its data only after the fenced snapshot commits."""

    try:
        issue = await fetch_github_issue(
            http_client=http_client,
            repository=repository,
            issue_number=issue_number,
        )

    except httpx.HTTPStatusError as exc:
        response = exc.response
        status_code = response.status_code

        if is_github_rate_limited(response):
            raise GitHubRateLimitError(
                not_before=get_github_retry_time(response)
            ) from exc

        if status_code >= 500 or status_code in (408, 409):
            raise RetryableInvestigationError(
                f"GitHub API temporary failure: HTTP {status_code}"
            ) from exc

        raise PermanentInvestigationError(
            f"GitHub API request failed: HTTP {status_code}"
        ) from exc

    except httpx.RequestError as exc:
        raise RetryableInvestigationError("GitHub API network request failed") from exc

    except ValueError as exc:
        raise PermanentInvestigationError(
            "GitHub issue response or input is invalid"
        ) from exc

    snapshot = IssueSnapshotData(
        title=issue.title,
        body=issue.body,
        state=issue.state,
        source_url=str(issue.html_url),
        source_created_at=issue.created_at,
        source_updated_at=issue.updated_at,
    )

    async with session_factory() as session:
        async with session.begin():
            saved = await upsert_issue_snapshot(
                session,
                task_id=task_id,
                claim_token=claim_token,
                snapshot=snapshot,
            )

            if not saved:
                raise LeaseLostError(
                    f"Investigation task {task_id} no longer owns a valid lease"
                )

    return snapshot
