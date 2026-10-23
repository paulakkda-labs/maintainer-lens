"""Read-only integration tests for repository vector index schema."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from maintainer_lens.core.config import get_settings


@pytest.mark.anyio
async def test_repository_vector_schema_exists() -> None:
    """Verify the migrated tables and pgvector column in PostgreSQL."""

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration tests are disabled")

    settings = get_settings()

    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )

    try:
        async with engine.connect() as connection:
            extension_exists = await connection.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector')"
                )
            )

            index_table_exists = await connection.scalar(
                text("SELECT to_regclass('repository_indexes') IS NOT NULL")
            )

            chunk_table_exists = await connection.scalar(
                text("SELECT to_regclass('repository_chunks') IS NOT NULL")
            )

            vector_type = await connection.scalar(
                text(
                    """
                    SELECT format_type(
                        attribute.atttypid,
                        attribute.atttypmod
                    )
                    FROM pg_attribute AS attribute
                    WHERE attribute.attrelid =
                          to_regclass('repository_chunks')
                      AND attribute.attname = 'embedding'
                      AND NOT attribute.attisdropped
                    """
                )
            )

        assert extension_exists is True
        assert index_table_exists is True
        assert chunk_table_exists is True
        assert vector_type == "vector"

    finally:
        await engine.dispose()
