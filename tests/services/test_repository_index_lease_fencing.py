"""Unit tests for repository index lease fencing requirements."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from maintainer_lens.services.repository_index_lease import (
    assert_repository_index_lease_owned,
)


@pytest.mark.anyio
async def test_fencing_requires_active_transaction() -> None:
    """Reject ownership checks outside a database transaction."""

    session = MagicMock()
    session.in_transaction.return_value = False

    with pytest.raises(
        RuntimeError,
        match="inside a transaction",
    ):
        await assert_repository_index_lease_owned(
            session,
            identity_key="a" * 64,
            owner_token=uuid4(),
        )

    session.scalar.assert_not_called()
