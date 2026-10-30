"""Strict GitHub Issue URL intake without accepting arbitrary fetch targets."""

import re

from pydantic import BaseModel, ConfigDict, field_validator

from maintainer_lens.agents.investigation_analysis import ResponseLanguage
from maintainer_lens.schemas.investigation_budget import KnowledgeBudgetProfile
from maintainer_lens.schemas.investigation_contracts import InvestigationIdentity


def parse_issue_url(value: str) -> InvestigationIdentity:
    """Extract a canonical identity; no user-supplied URL is fetched directly."""
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?",
        value,
    )
    if match is None:
        raise ValueError("Expected https://github.com/owner/repository/issues/number")
    return InvestigationIdentity(repository=match[1], issue_number=int(match[2]))


class CreateInvestigationFromUrlRequest(BaseModel):
    """URL-only automatic preparation, with a backend-defined budget profile."""

    model_config = ConfigDict(extra="forbid", strict=True)

    issue_url: str
    response_language: ResponseLanguage = "en"
    knowledge_budget_profile: KnowledgeBudgetProfile = "standard"

    @field_validator("issue_url")
    @classmethod
    def validate_issue_url(cls, value: str) -> str:
        parse_issue_url(value)
        return value
