"""FastAPI dependencies for shared application resources."""

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def get_db_session(
    request: Request,
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a request-scoped asynchronous database session."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_session_factory(
    request: Request,
) -> async_sessionmaker[AsyncSession]:
    """Provide the application-scoped database session factory."""
    return request.app.state.session_factory
