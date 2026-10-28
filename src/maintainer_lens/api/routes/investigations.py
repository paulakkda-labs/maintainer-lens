"""HTTP endpoints for submitting and querying investigations."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.agents.investigation.state import InvalidInvestigationResultError
from maintainer_lens.api.dependencies import (
    get_db_session,
    get_session_factory,
)
from maintainer_lens.db.investigation_queries import (
    get_investigation_details,
)
from maintainer_lens.db.investigation_results import (
    StoredInvestigationResultError,
    decode_investigation_result,
)
from maintainer_lens.schemas.investigation_api import (
    CreateInvestigationRequest,
    CreateInvestigationResponse,
    InvestigationDetailResponse,
    IssueSnapshotResponse,
)
from maintainer_lens.schemas.investigation_budget import (
    KnowledgePreparationBudget,
    list_knowledge_budget_profiles,
)
from maintainer_lens.schemas.investigation_url import (
    CreateInvestigationFromUrlRequest,
    parse_issue_url,
)
from maintainer_lens.services.investigation_report import render_investigation_report
from maintainer_lens.services.investigation_submission import (
    create_investigation_submission,
)

router = APIRouter(prefix="/investigations", tags=["Investigations"])


@router.post(
    "",
    response_model=CreateInvestigationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_investigation(
    payload: CreateInvestigationRequest,
    session_factory: Annotated[
        async_sessionmaker[AsyncSession],
        Depends(get_session_factory),
    ],
) -> CreateInvestigationResponse:
    """Accept a new investigation and persist its outbox event."""

    message = await create_investigation_submission(
        session_factory,
        repository=payload.repository,
        issue_number=payload.issue_number,
        source_index_id=payload.source_index_id,
        knowledge_budget_profile=payload.knowledge_budget_profile,
    )

    return CreateInvestigationResponse(
        task_id=message.task_id,
        status="pending",
    )


@router.post(
    "/from-url",
    response_model=CreateInvestigationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_investigation_from_url(
    payload: CreateInvestigationFromUrlRequest,
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_session_factory)
    ],
) -> CreateInvestigationResponse:
    """Queue automatic preparation; return before any GitHub or model requests."""
    identity = parse_issue_url(payload.issue_url)
    message = await create_investigation_submission(
        session_factory,
        repository=identity.repository,
        issue_number=identity.issue_number,
        auto_prepare_knowledge=True,
        knowledge_budget_profile=payload.knowledge_budget_profile,
    )
    return CreateInvestigationResponse(task_id=message.task_id, status="pending")


@router.get("/budget-profiles", response_model=list[KnowledgePreparationBudget])
async def get_knowledge_budget_profiles() -> list[KnowledgePreparationBudget]:
    """Describe preparation limits without loading settings or opening resources."""
    return list_knowledge_budget_profiles()


@router.get(
    "/{task_id}",
    response_model=InvestigationDetailResponse,
)
async def get_investigation(
    task_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> InvestigationDetailResponse:
    """Return artifacts, exposing a Graph result only after task success."""

    result = await get_investigation_details(
        session,
        task_id,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Investigation not found",
        )

    task, snapshot, stored_result = result

    try:
        knowledge_budget = KnowledgePreparationBudget.model_validate(
            task.knowledge_budget
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored investigation budget is unavailable",
        ) from exc

    graph_result = None
    report_markdown = None
    if task.status == "succeeded" and stored_result is not None:
        try:
            if task.auto_prepare_knowledge and task.prepared_source_index_id is None:
                raise StoredInvestigationResultError(
                    "Automatic task has no bound index"
                )
            graph_result = decode_investigation_result(
                stored_result,
                task_id=task.id,
                repository=task.repository,
                issue_number=task.issue_number,
                source_index_id=(
                    task.prepared_source_index_id
                    if task.auto_prepare_knowledge
                    else task.source_index_id
                ),
            )
            report_markdown = render_investigation_report(graph_result)
        except (StoredInvestigationResultError, InvalidInvestigationResultError) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Stored investigation result is unavailable",
            ) from exc

    issue = (
        IssueSnapshotResponse.model_validate(
            snapshot,
            from_attributes=True,
        )
        if snapshot is not None
        else None
    )

    return InvestigationDetailResponse(
        task_id=task.id,
        repository=task.repository,
        issue_number=task.issue_number,
        source_index_id=task.source_index_id,
        auto_prepare_knowledge=bool(task.auto_prepare_knowledge),
        prepared_source_index_id=task.prepared_source_index_id,
        knowledge_budget=knowledge_budget,
        status=task.status,
        issue=issue,
        result=graph_result,
        report_markdown=report_markdown,
    )
