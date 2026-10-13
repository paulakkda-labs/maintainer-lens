"""Asynchronous database engine configuration."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_db_engine(database_url: str) -> AsyncEngine:
    """Create an asynchronous database engine with connection pooling."""
    engine = create_async_engine(
        database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )

    return engine
