"""PostgreSQL integration tests for repository index lease lifecycle."""

import os
from secrets import token_hex
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from maintainer_lens.core.config import get_settings
from maintainer_lens.db.models.repository_index_lease import (
    RepositoryIndexBuildLease,
)
from maintainer_lens.schemas.repository import RepositoryRevision
from maintainer_lens.services.repository_index_lease import (
    RepositoryIndexLeaseLost,
    assert_repository_index_lease_owned,
    make_repository_index_identity_key,
    release_repository_index_lease,
    renew_repository_index_lease,
    try_acquire_repository_index_lease,
)


@pytest.mark.anyio
async def test_repository_index_lease_lifecycle() -> None:
    """Protect renewal, release, and ownership after lease takeover."""

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
    }

    identity_key = make_repository_index_identity_key(
        revision,
        **options,
    )

    async def acquire() -> UUID | None:
        async with session_factory() as session:
            return await try_acquire_repository_index_lease(
                session,
                revision,
                **options,
                lease_seconds=300,
            )

    async def renew(token: UUID) -> bool:
        async with session_factory() as session:
            return await renew_repository_index_lease(
                session,
                identity_key=identity_key,
                owner_token=token,
                lease_seconds=300,
            )

    async def release(token: UUID) -> bool:
        async with session_factory() as session:
            return await release_repository_index_lease(
                session,
                identity_key=identity_key,
                owner_token=token,
            )

    async def check_ownership(token: UUID) -> None:
        async with session_factory() as session:
            async with session.begin():
                await assert_repository_index_lease_owned(
                    session,
                    identity_key=identity_key,
                    owner_token=token,
                )

    try:
        original_token = await acquire()
        assert isinstance(original_token, UUID)

        # An active owner can renew and pass ownership checks.
        assert await renew(original_token) is True
        await check_ownership(original_token)

        # An unrelated token cannot renew or release the lease.
        unrelated_token = uuid4()
        assert await renew(unrelated_token) is False
        assert await release(unrelated_token) is False

        with pytest.raises(RepositoryIndexLeaseLost):
            await check_ownership(unrelated_token)

        # Expire the lease directly in PostgreSQL.
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

        # An expired owner cannot renew or pass fencing.
        assert await renew(original_token) is False

        with pytest.raises(RepositoryIndexLeaseLost):
            await check_ownership(original_token)

        # Another worker takes over the expired lease.
        replacement_token = await acquire()
        assert isinstance(replacement_token, UUID)
        assert replacement_token != original_token

        # The stale worker must not release the new owner's lease.
        assert await release(original_token) is False
        assert await renew(original_token) is False

        with pytest.raises(RepositoryIndexLeaseLost):
            await check_ownership(original_token)

        # Only the new owner can renew, validate, and release.
        assert await renew(replacement_token) is True
        await check_ownership(replacement_token)
        assert await release(replacement_token) is True

        # Releasing the lease allows a fresh acquisition.
        next_token = await acquire()
        assert isinstance(next_token, UUID)
        assert next_token != replacement_token

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
