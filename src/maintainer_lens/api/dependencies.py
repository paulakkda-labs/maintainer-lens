"""FastAPI dependencies for shared application resources."""

from collections.abc import AsyncGenerator

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db_session(
    request: Request,
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a request-scoped asynchronous database session."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


async def get_redis_client(
    request: Request,
) -> Redis:
    """Return the application-scoped asynchronous Redis client."""
    redis_client = request.app.state.redis_client
    return redis_client
