"""Shared PostgreSQL fixtures for integration tests.

Each test owns its database engine and independent task ID. Concurrent workers
create separate sessions from the same session factory.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.investigation_tasks import create_investigation_task
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.session import create_session_factory


@pytest.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Provide a fresh database engine and dispose of it after the test."""
    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create separate database sessions without sharing transactions."""
    return create_session_factory(db_engine)


@pytest.fixture
async def task_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    """Supply a unique task ID and delete any persisted task on teardown."""
    generated_id = str(uuid4())
    try:
        yield generated_id
    finally:
        async with session_factory() as session:
            async with session.begin():
                stmt = delete(InvestigationTask).where(
                    InvestigationTask.id == generated_id
                )
                await session.execute(stmt)


@pytest.fixture
async def investigation_task(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    task_id: str,
) -> tuple[async_sessionmaker[AsyncSession], str, AsyncEngine]:
    """Create a pending task for worker tests; task_id handles cleanup."""
    async with session_factory() as session:
        async with session.begin():
            await create_investigation_task(
                session=session,
                task_id=task_id,
                repository="fastapi/fastapi",
                issue_number=123,
                response_language="zh",
            )
    return session_factory, task_id, db_engine
