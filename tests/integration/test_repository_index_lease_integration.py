"""PostgreSQL integration tests for repository index lease claims."""

import os
from asyncio import TaskGroup
from secrets import token_hex
from uuid import UUID

import pytest
from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maintainer_lens.core.config import get_settings
from maintainer_lens.db.models.repository_index_lease import (
    RepositoryIndexBuildLease,
)
from maintainer_lens.schemas.repository import RepositoryRevision
from maintainer_lens.services.repository_index_lease import (
    make_repository_index_identity_key,
    try_acquire_repository_index_lease,
)


@pytest.mark.anyio
async def test_concurrent_claim_and_expired_takeover() -> None:
    """Allow one active owner and a new owner after lease expiration."""

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration tests are disabled")

    settings = get_settings()

    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
    )

    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )

    revision = RepositoryRevision(
        repository="pypa/sampleproject",
        default_branch="main",
        commit_sha=token_hex(20),
        tree_sha=token_hex(20),
    )

    options = {
        "embedding_provider_name": "test-provider",
        "embedding_model": "test-model",
        "embedding_dimensions": 1024,
        "chunker_version": "test-v1",
        "lease_seconds": 300,
    }

    identity_key = make_repository_index_identity_key(
        revision,
        embedding_provider_name="test-provider",
        embedding_model="test-model",
        embedding_dimensions=1024,
        chunker_version="test-v1",
    )

    async def acquire() -> UUID | None:
        """Attempt a claim using an independent database session."""

        async with session_factory() as session:
            return await try_acquire_repository_index_lease(
                session,
                revision,
                **options,
            )

    try:
        async with TaskGroup() as group:
            first_task = group.create_task(acquire())
            second_task = group.create_task(acquire())

        first = first_task.result()
        second = second_task.result()

        successful_tokens = [token for token in (first, second) if token is not None]

        assert len(successful_tokens) == 1

        original_token = successful_tokens[0]
        assert isinstance(original_token, UUID)

        # An active lease must reject another claimant.
        assert await acquire() is None

        # Expire the test lease without waiting in real time.
        async with engine.begin() as connection:
            await connection.execute(
                update(RepositoryIndexBuildLease)
                .where(RepositoryIndexBuildLease.identity_key == identity_key)
                .values(
                    expires_at=(
                        func.clock_timestamp() - func.make_interval(0, 0, 0, 0, 0, 0, 1)
                    )
                )
            )

        replacement_token = await acquire()

        assert isinstance(replacement_token, UUID)
        assert replacement_token != original_token

        # The replacement becomes the exclusive owner.
        assert await acquire() is None

    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    delete(RepositoryIndexBuildLease).where(
                        RepositoryIndexBuildLease.identity_key == identity_key
                    )
                )
        finally:
            await engine.dispose()
