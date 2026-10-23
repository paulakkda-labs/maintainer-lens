"""Manage exclusive repository index build leases."""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.db.models.repository_index_lease import (
    RepositoryIndexBuildLease,
)
from maintainer_lens.schemas.repository import RepositoryRevision


class RepositoryIndexLeaseLost(RuntimeError):
    """The caller no longer owns an active repository index lease."""


def make_repository_index_identity_key(
    revision: RepositoryRevision,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    chunker_version: str,
) -> str:
    """Generate a deterministic key for a repository index identity."""

    payload = {
        "repository": revision.repository,
        "commit_sha": revision.commit_sha,
        "embedding_provider": embedding_provider_name,
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "chunker_version": chunker_version,
    }

    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def try_acquire_repository_index_lease(
    session: AsyncSession,
    revision: RepositoryRevision,
    *,
    embedding_provider_name: str,
    embedding_model: str,
    embedding_dimensions: int,
    chunker_version: str,
    lease_seconds: int = 300,
) -> UUID | None:
    """Acquire a repository index build lease if available."""

    if embedding_dimensions <= 0:
        raise ValueError("Embedding dimensions must be positive")

    if lease_seconds <= 0:
        raise ValueError("Lease duration must be positive")

    if any(
        not value.strip()
        for value in (
            embedding_provider_name,
            embedding_model,
            chunker_version,
        )
    ):
        raise ValueError("Index configuration fields cannot be empty")

    identity_key = make_repository_index_identity_key(
        revision,
        embedding_provider_name=embedding_provider_name,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
        chunker_version=chunker_version,
    )

    candidate_token = uuid4()

    interval = func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)

    new_expiration = func.clock_timestamp() + interval

    statement = insert(RepositoryIndexBuildLease).values(
        identity_key=identity_key,
        owner_token=candidate_token,
        expires_at=new_expiration,
    )

    statement = statement.on_conflict_do_update(
        index_elements=[RepositoryIndexBuildLease.identity_key],
        set_={
            "owner_token": candidate_token,
            "expires_at": new_expiration,
        },
        where=(RepositoryIndexBuildLease.expires_at <= func.clock_timestamp()),
    ).returning(RepositoryIndexBuildLease.owner_token)

    async with session.begin():
        result = await session.execute(statement)
        acquired_token = result.scalar_one_or_none()

    return acquired_token


async def renew_repository_index_lease(
    session: AsyncSession,
    *,
    identity_key: str,
    owner_token: UUID,
    lease_seconds: int = 300,
) -> bool:
    """Extend an active lease only for its current owner."""

    if lease_seconds <= 0:
        raise ValueError("Lease duration must be positive")

    interval = func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds)

    statement = (
        update(RepositoryIndexBuildLease)
        .where(
            RepositoryIndexBuildLease.identity_key == identity_key,
            RepositoryIndexBuildLease.owner_token == owner_token,
            RepositoryIndexBuildLease.expires_at > func.clock_timestamp(),
        )
        .values(expires_at=func.clock_timestamp() + interval)
        .returning(RepositoryIndexBuildLease.owner_token)
    )

    async with session.begin():
        result = await session.execute(statement)
        renewed_token = result.scalar_one_or_none()

    return renewed_token is not None


async def release_repository_index_lease(
    session: AsyncSession,
    *,
    identity_key: str,
    owner_token: UUID,
) -> bool:
    """Release a lease only when its owner token matches."""

    statement = (
        delete(RepositoryIndexBuildLease)
        .where(
            RepositoryIndexBuildLease.identity_key == identity_key,
            RepositoryIndexBuildLease.owner_token == owner_token,
        )
        .returning(RepositoryIndexBuildLease.owner_token)
    )

    async with session.begin():
        result = await session.execute(statement)
        released_token = result.scalar_one_or_none()

    return released_token is not None


async def assert_repository_index_lease_owned(
    session: AsyncSession,
    *,
    identity_key: str,
    owner_token: UUID,
) -> None:
    """Lock and validate lease ownership within an existing transaction."""

    if not session.in_transaction():
        raise RuntimeError("Lease ownership must be checked inside a transaction")

    statement = (
        select(RepositoryIndexBuildLease.owner_token)
        .where(
            RepositoryIndexBuildLease.identity_key == identity_key,
            RepositoryIndexBuildLease.owner_token == owner_token,
            RepositoryIndexBuildLease.expires_at > func.clock_timestamp(),
        )
        .with_for_update()
    )

    actual_token = await session.scalar(statement)

    if actual_token is None:
        raise RepositoryIndexLeaseLost(
            "Repository index build lease is no longer owned"
        )
