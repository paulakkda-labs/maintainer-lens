"""Tests for retryable investigation execution."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import maintainer_lens.investigation_runner as runner


@pytest.fixture
def mocked_db(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, MagicMock, AsyncMock, AsyncMock, AsyncMock]:
    """Mock database sessions and investigation state operations."""
    session = MagicMock()
    session.__aenter__.return_value = session

    session_factory = MagicMock(return_value=session)

    claim = AsyncMock(return_value="claim-token-001")
    release = AsyncMock(return_value=True)
    succeed = AsyncMock(return_value=True)

    monkeypatch.setattr(
        runner,
        "claim_investigation_task",
        claim,
    )
    monkeypatch.setattr(
        runner,
        "release_investigation_task_for_retry",
        release,
    )
    monkeypatch.setattr(
        runner,
        "mark_investigation_succeeded",
        succeed,
    )

    return session_factory, session, claim, release, succeed


@pytest.mark.anyio
async def test_successful_investigation(
    mocked_db: tuple[MagicMock, MagicMock, AsyncMock, AsyncMock, AsyncMock],
) -> None:
    """Mark a successful investigation as succeeded."""
    session_factory, session, claim, release, succeed = mocked_db

    processor = AsyncMock()

    await runner.execute_investigation_task(
        session_factory=session_factory,
        task_id="task-001",
        processor=processor,
    )

    processor.assert_awaited_once()

    claim.assert_awaited_once_with(
        session=session,
        task_id="task-001",
    )

    succeed.assert_awaited_once_with(
        session=session,
        task_id="task-001",
        claim_token="claim-token-001",
    )

    release.assert_not_awaited()


@pytest.mark.anyio
async def test_retryable_failure_releases_task(
    mocked_db: tuple[MagicMock, MagicMock, AsyncMock, AsyncMock, AsyncMock],
) -> None:
    """Release a task and propagate its original retryable error."""
    session_factory, session, _, release, succeed = mocked_db

    original_error = runner.RetryableInvestigationError("GitHub API timeout")

    processor = AsyncMock(side_effect=original_error)

    with pytest.raises(runner.RetryableInvestigationError) as exc_info:
        await runner.execute_investigation_task(
            session_factory=session_factory,
            task_id="task-001",
            processor=processor,
        )

    assert exc_info.value is original_error

    release.assert_awaited_once_with(
        session=session,
        task_id="task-001",
        claim_token="claim-token-001",
    )

    succeed.assert_not_awaited()

    # Claim and release each use a transaction.
    assert session.begin.call_count == 2
    assert session.begin.return_value.__aexit__.await_count == 2


@pytest.mark.anyio
async def test_release_failure_raises_lease_lost(
    mocked_db: tuple[MagicMock, MagicMock, AsyncMock, AsyncMock, AsyncMock],
) -> None:
    """Do not treat an invalid claim as a successful release."""
    session_factory, _, _, release, succeed = mocked_db

    release.return_value = False

    processor = AsyncMock(
        side_effect=runner.RetryableInvestigationError("GitHub API timeout")
    )

    with pytest.raises(
        runner.LeaseLostError,
        match="Cannot update investigation",
    ):
        await runner.execute_investigation_task(
            session_factory=session_factory,
            task_id="task-001",
            processor=processor,
        )

    release.assert_awaited_once()
    succeed.assert_not_awaited()


@pytest.mark.anyio
async def test_release_commit_failure_propagates(
    mocked_db: tuple[MagicMock, MagicMock, AsyncMock, AsyncMock, AsyncMock],
) -> None:
    """Do not report a retryable failure before release commit succeeds."""
    session_factory, session, _, release, succeed = mocked_db

    commit_error = RuntimeError("Database commit failed")

    # First transaction (claim) succeeds.
    # Second transaction (release) fails on exit.
    session.begin.return_value.__aexit__.side_effect = [
        False,
        commit_error,
    ]

    processor = AsyncMock(
        side_effect=runner.RetryableInvestigationError("GitHub API timeout")
    )

    with pytest.raises(RuntimeError) as exc_info:
        await runner.execute_investigation_task(
            session_factory=session_factory,
            task_id="task-001",
            processor=processor,
        )

    assert exc_info.value is commit_error

    release.assert_awaited_once()
    succeed.assert_not_awaited()
