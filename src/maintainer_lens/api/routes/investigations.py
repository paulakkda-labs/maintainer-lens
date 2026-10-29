"""HTTP endpoints for submitting and querying investigations."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.agents.investigation.state import InvalidInvestigationResultError
from maintainer_lens.api.dependencies import (
    get_db_session,
    get_session_factory,
)
from maintainer_lens.api.routes.investigation_reviews import router as review_router
from maintainer_lens.db.investigation_progress import (
    read_latest_progress,
    read_task_events,
)
from maintainer_lens.db.investigation_queries import (
    find_investigation_tasks,
    get_investigation_details,
)
from maintainer_lens.db.investigation_results import (
    StoredInvestigationResultError,
    decode_investigation_result,
)
from maintainer_lens.db.investigation_reuse import (
    get_previous_report_task,
    has_active_url_check,
)
from maintainer_lens.schemas.investigation_api import (
    CreateInvestigationRequest,
    CreateInvestigationResponse,
    InvestigationDetailResponse,
    InvestigationLookupPage,
    InvestigationTaskSummary,
    IssueSnapshotResponse,
)
from maintainer_lens.schemas.investigation_budget import (
    KnowledgePreparationBudget,
    list_knowledge_budget_profiles,
)
from maintainer_lens.schemas.investigation_progress import InvestigationEventPage
from maintainer_lens.schemas.investigation_reuse import (
    InvestigationFreshness,
    ResolveInvestigationResponse,
)
from maintainer_lens.schemas.investigation_url import (
    CreateInvestigationFromUrlRequest,
    parse_issue_url,
)
from maintainer_lens.services.investigation_freshness import (
    historical_freshness,
    render_observed_status,
)
from maintainer_lens.services.investigation_report import render_investigation_report
from maintainer_lens.services.investigation_submission import (
    create_investigation_submission,
)
from maintainer_lens.services.investigation_url_reuse import (
    report_for_task,
    resolve_investigation_url,
    stored_freshness,
)

router = APIRouter(prefix="/investigations", tags=["Investigations"])
router.include_router(review_router)


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
        response_language=payload.response_language,
    )

    return CreateInvestigationResponse(
        task_id=message.task_id,
        status="pending",
    )


@router.post(
    "/from-url",
    response_model=ResolveInvestigationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        200: {
            "model": ResolveInvestigationResponse,
            "description": "Existing report or terminal investigation",
        }
    },
)
async def create_investigation_from_url(
    payload: CreateInvestigationFromUrlRequest,
    response: Response,
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_session_factory)
    ],
) -> ResolveInvestigationResponse:
    """Resolve existing work, scoped freshness, recovery or one new investigation."""
    identity = parse_issue_url(payload.issue_url)
    try:
        result = await resolve_investigation_url(
            session_factory,
            repository=identity.repository,
            issue_number=identity.issue_number,
            knowledge_budget_profile=payload.knowledge_budget_profile,
            response_language=payload.response_language,
        )
    except (ValueError, StoredInvestigationResultError) as exc:
        raise HTTPException(
            status_code=500, detail="Stored investigation is unavailable"
        ) from exc
    response.status_code = (
        202
        if result.decision
        in ("created", "active", "resumed", "checking", "awaiting_review")
        else 200
    )
    return result


@router.get("/budget-profiles", response_model=list[KnowledgePreparationBudget])
async def get_knowledge_budget_profiles() -> list[KnowledgePreparationBudget]:
    """Describe preparation limits without loading settings or opening resources."""
    return list_knowledge_budget_profiles()


@router.get(
    "/lookup",
    response_model=InvestigationLookupPage,
)
async def lookup_investigations(
    issue_url: Annotated[str, Query(max_length=2048)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    before: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> InvestigationLookupPage:
    """Locate existing tasks without GitHub calls, freshness checks or dispatch."""
    try:
        identity = parse_issue_url(issue_url)
        if identity.issue_number > 2_147_483_647:
            raise ValueError("Issue number exceeds the task storage range")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid GitHub Issue URL") from exc
    try:
        tasks, next_before = await find_investigation_tasks(
            session,
            repository=identity.repository,
            issue_number=identity.issue_number,
            before=before,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid investigation page"
        ) from exc
    return InvestigationLookupPage(
        repository=identity.repository,
        issue_number=identity.issue_number,
        tasks=[
            InvestigationTaskSummary(
                task_id=task.id,
                response_language=getattr(task, "response_language", None) or "zh",
                status=task.status,
                generation=task.execution_generation,
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
            for task in tasks
        ],
        next_before=next_before,
    )


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
            if graph_result.response_language != (
                getattr(task, "response_language", None) or "zh"
            ):
                raise StoredInvestigationResultError(
                    "Stored report language differs from its task"
                )
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

    progress, last_error = None, None
    if getattr(task, "execution_generation", None) is not None:
        try:
            progress, last_error = await read_latest_progress(
                session,
                task.id,
                generation=task.execution_generation,
                task_status=task.status,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=500, detail="Stored investigation progress is unavailable"
            ) from exc
    try:
        freshness = (
            stored_freshness(task)
            if getattr(task, "freshness_payload", None) is not None
            else InvestigationFreshness()
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=500, detail="Stored investigation freshness is unavailable"
        ) from exc
    if freshness.status == "checking" and not await has_active_url_check(
        session, task.id
    ):
        freshness = freshness.model_copy(
            update={
                "status": "unknown",
                "reason_code": "check_expired",
                "message": "No active metadata check remains; latest status is unknown.",
            }
        )
    if (
        report_markdown is not None
        and getattr(task, "freshness_payload", None) is not None
    ):
        report_markdown = render_observed_status(
            report_markdown,
            graph_result.response_language,
            state=freshness.observed_issue_state
            or (snapshot.state if snapshot else None),
            checked_at=freshness.checked_at
            if freshness.observed_issue_state
            else (snapshot.fetched_at if snapshot else None),
            freshness=freshness,
        )
    previous_report = None
    if getattr(task, "freshness_payload", None) is not None:
        try:
            previous = await get_previous_report_task(session, task.id)
            if previous is not None:
                if (
                    previous.repository.casefold(),
                    previous.issue_number,
                    previous.knowledge_budget,
                    getattr(previous, "response_language", None) or "zh",
                ) != (
                    task.repository.casefold(),
                    task.issue_number,
                    task.knowledge_budget,
                    getattr(task, "response_language", None) or "zh",
                ):
                    raise HTTPException(
                        status_code=500, detail="Stored previous report is unavailable"
                    )
                previous_report = await report_for_task(
                    session,
                    previous,
                    freshness_override=historical_freshness(freshness),
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=500, detail="Stored previous report is unavailable"
            ) from exc
    return InvestigationDetailResponse(
        task_id=task.id,
        response_language=getattr(task, "response_language", None) or "zh",
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
        generation=getattr(task, "execution_generation", 0) or 0,
        progress=progress,
        last_error=last_error,
        freshness=freshness,
        previous_report_task_id=previous_report[0] if previous_report else None,
        previous_report_markdown=previous_report[1] if previous_report else None,
    )


@router.get("/{task_id}/events", response_model=InvestigationEventPage)
async def get_investigation_events(
    task_id: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> InvestigationEventPage:
    if await get_investigation_details(session, task_id) is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return await read_task_events(session, task_id, after=after, limit=limit)
