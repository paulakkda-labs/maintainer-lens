"""Operator decisions for on-demand investigation reviews; no provider calls."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.api.dependencies import get_db_session, get_session_factory
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.investigation_review import read_reviews
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.schemas.investigation_review import ReviewDecision, ReviewResponse
from maintainer_lens.services.investigation_configuration import (
    make_worker_execution_config,
)
from maintainer_lens.services.investigation_review import (
    ReviewConflictError,
    ReviewNotFoundError,
    decide_review,
    review_response,
)

router = APIRouter()


@router.get("/{task_id}/reviews", response_model=list[ReviewResponse])
async def get_investigation_reviews(
    task_id: str, session: Annotated[AsyncSession, Depends(get_db_session)]
):
    task = await session.get(InvestigationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    settings = get_settings()
    try:
        return [
            review_response(
                row, task, output_ceiling=settings.llm_review_max_output_tokens
            )
            for row in await read_reviews(session, task_id)
        ]
    except ValueError as exc:
        raise HTTPException(
            status_code=500, detail="Stored review is unavailable"
        ) from exc


@router.post("/{task_id}/reviews/{review_id}/decision", response_model=ReviewResponse)
async def submit_investigation_review_decision(
    task_id: str,
    review_id: UUID,
    decision: ReviewDecision,
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_session_factory)
    ],
):
    settings = get_settings()
    try:
        return await decide_review(
            session_factory,
            task_id=task_id,
            review_id=review_id,
            decision=decision,
            configuration_factory=lambda source: make_worker_execution_config(
                settings, source_enabled=source
            ),
            output_ceiling=settings.llm_review_max_output_tokens,
        )
    except ReviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Review request not found") from exc
    except ReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Invalid review budget or stored review data"
        ) from exc
