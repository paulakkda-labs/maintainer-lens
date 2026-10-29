"""Bounded GitHub metadata observations; no embeddings or model calls."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from maintainer_lens.clients.github import GitHubIssue, fetch_github_issue
from maintainer_lens.clients.github_http import create_github_http_client
from maintainer_lens.clients.github_repository import resolve_repository_revision
from maintainer_lens.core.config import get_settings
from maintainer_lens.schemas.investigation_reuse import InvestigationFreshness

FRESHNESS_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class IssueObservation:
    issue: GitHubIssue
    commit_sha: str
    checked_at: datetime


async def observe_issue(repository: str, issue_number: int) -> IssueObservation:
    """Use the existing client factory; its request lifetime is owned here."""
    settings = get_settings()
    async with create_github_http_client(settings.github_token) as client:
        issue = await fetch_github_issue(client, repository, issue_number)
        revision = await resolve_repository_revision(client, repository)
    return IssueObservation(issue, revision.commit_sha, datetime.now(UTC))


async def bounded_observation(observer, repository: str, issue_number: int):
    scope = asyncio.timeout(FRESHNESS_TIMEOUT_SECONDS)
    try:
        async with scope:
            result = await observer(repository, issue_number)
        if scope.expired():
            raise TimeoutError("Metadata deadline expired")
        if not isinstance(result, IssueObservation):
            raise ValueError("Invalid metadata observation")
        InvestigationFreshness(
            status="current",
            checked_at=result.checked_at,
            observed_issue_state=result.issue.state,
            observed_commit_sha=result.commit_sha,
        )
        return result, None
    except (TimeoutError, httpx.HTTPError, ValueError):
        # Cancellation is deliberately not caught. A failed check must not become
        # a claim that old content is current, nor expose provider response text.
        return None, InvestigationFreshness(
            status="unknown",
            checked_at=datetime.now(UTC),
            reason_code="metadata_unavailable",
            message="Latest Issue and source metadata could not be verified.",
        )


def render_observed_status(
    markdown: str,
    language: str,
    *,
    state: str | None,
    checked_at: datetime | None,
    freshness: InvestigationFreshness | None = None,
) -> str:
    prefix = ""
    if state is not None:
        observed = checked_at.isoformat() if checked_at is not None else "unknown"
        if language == "zh":
            label = "已关闭" if state == "closed" else "打开中"
            prefix = f"## GitHub 观察状态\n\nIssue：**{label}**。观察时间：{observed}。\n\nGitHub 状态优先展示；是否存在代码修复及其适用版本，请结合下文证据判断。\n\n"
        else:
            label = "Closed" if state == "closed" else "Open"
            prefix = f"## Observed GitHub status\n\nIssue: **{label}**. Observed at: {observed}.\n\nGitHub status takes display priority; technical fix evidence and applicable revisions are discussed below.\n\n"
    if freshness is not None:
        zh = {
            "not_checked": "分析针对生成时的输入，尚未单独复核最新状态。",
            "checking": "正在核对最新状态；下方为此前保存的分析。",
            "current": "已在标示时间核对 Issue 元数据和默认分支版本。",
            "stale": "下方为历史分析；当前输入已变化或更新调查尚未完成。",
            "unknown": "最新状态检查未完成，无法确认下方报告仍然适用。",
        }
        en = {
            "not_checked": "Analysis reflects its original inputs; latest status has not been separately checked.",
            "checking": "Latest status is being checked; the saved analysis is shown below.",
            "current": "Issue metadata and the default-branch revision were checked at the stated time.",
            "stale": "Historical analysis is shown; inputs changed or the updated investigation is unfinished.",
            "unknown": "The latest check could not complete; current applicability is unknown.",
        }
        scope = (
            "检查范围不包含所有关联或未关联 PR/讨论的变化。"
            if language == "zh"
            else "This check does not cover all linked or unlinked PR/discussion changes."
        )
        prefix += f"{(zh if language == 'zh' else en)[freshness.status]} {scope}\n\n"
    return prefix + markdown


def historical_freshness(value: InvestigationFreshness) -> InvestigationFreshness:
    if value.status not in ("current", "not_checked"):
        return value
    return value.model_copy(
        update={
            "status": "stale",
            "reason_code": "previous_report",
            "message": "A historical report is shown while the selected investigation is unfinished.",
        }
    )
