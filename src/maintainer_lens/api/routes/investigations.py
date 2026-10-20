"""HTTP endpoints for submitting and querying investigations."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.api.dependencies import (
    get_db_session,
    get_session_factory,
)
from maintainer_lens.db.investigation_queries import (
    get_investigation_with_snapshot,
)
from maintainer_lens.schemas.investigation_api import (
    CreateInvestigationRequest,
    CreateInvestigationResponse,
    InvestigationDetailResponse,
    IssueSnapshotResponse,
)
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
    )

    return CreateInvestigationResponse(
        task_id=message.task_id,
        status="pending",
    )


@router.get(
    "/{task_id}",
    response_model=InvestigationDetailResponse,
)
async def get_investigation(
    task_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> InvestigationDetailResponse:
    """Return the investigation status and available issue snapshot."""

    result = await get_investigation_with_snapshot(
        session,
        task_id,
    )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Investigation not found",
        )

    task, snapshot = result

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
        status=task.status,
        issue=issue,
    )
