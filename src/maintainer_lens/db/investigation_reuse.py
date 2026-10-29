"""Transaction-scoped URL arbitration; external checks run after this scope exits."""

import hashlib
import json
from contextlib import asynccontextmanager

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.models.investigation_reuse import InvestigationUrlLookup


def url_intent_key(repository: str, issue_number: int, budget: dict) -> str:
    payload = {
        "repository": repository.casefold(),
        "issue_number": issue_number,
        "budget": budget,
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class InvestigationUrlStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    @asynccontextmanager
    async def locked(self, repository: str, issue_number: int, budget: dict):
        key = url_intent_key(repository, issue_number, budget)
        async with self.session_factory() as session, session.begin():
            await session.execute(
                insert(InvestigationUrlLookup)
                .values(
                    intent_key=key,
                    repository=repository.casefold(),
                    issue_number=issue_number,
                    knowledge_budget=budget,
                )
                .on_conflict_do_nothing(index_elements=["intent_key"])
            )
            lookup = await session.scalar(
                select(InvestigationUrlLookup)
                .where(InvestigationUrlLookup.intent_key == key)
                .with_for_update()
            )
            if (
                lookup.repository != repository.casefold()
                or lookup.issue_number != issue_number
                or lookup.knowledge_budget != budget
            ):
                raise ValueError("Stored URL lookup identity mismatch")
            task = None
            if lookup.task_id is not None:
                task = await session.scalar(
                    select(InvestigationTask)
                    .where(InvestigationTask.id == lookup.task_id)
                    .with_for_update()
                )
            if task is None:
                # Adopt pre-feature automatic tasks without manufacturing a new
                # investigation just because the URL lookup table is new.
                task = await session.scalar(
                    select(InvestigationTask)
                    .where(
                        func.lower(InvestigationTask.repository)
                        == repository.casefold(),
                        InvestigationTask.issue_number == issue_number,
                        InvestigationTask.auto_prepare_knowledge.is_(True),
                        InvestigationTask.knowledge_budget == budget,
                    )
                    .order_by(
                        InvestigationTask.created_at.desc(), InvestigationTask.id.desc()
                    )
                    .limit(1)
                    .with_for_update()
                )
                if task is not None:
                    lookup.task_id = task.id
            if task is not None and (
                task.repository.casefold() != repository.casefold()
                or task.issue_number != issue_number
                or not task.auto_prepare_knowledge
                or task.knowledge_budget != budget
            ):
                raise ValueError(
                    "URL lookup points to a different investigation intent"
                )
            yield session, lookup, task


async def get_previous_report_task(session, task_id: str):
    anchor = await session.scalar(
        select(InvestigationUrlLookup.report_task_id)
        .where(
            InvestigationUrlLookup.task_id == task_id,
            InvestigationUrlLookup.report_task_id != task_id,
        )
        .limit(1)
    )
    return await session.get(InvestigationTask, anchor) if anchor else None


async def has_active_url_check(session, task_id: str) -> bool:
    active = await session.scalar(
        select(InvestigationUrlLookup.intent_key)
        .where(
            InvestigationUrlLookup.task_id == task_id,
            InvestigationUrlLookup.check_token.is_not(None),
            InvestigationUrlLookup.check_expires_at > func.clock_timestamp(),
        )
        .limit(1)
    )
    return active is not None
