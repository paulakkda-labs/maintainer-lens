"""Unit tests for asynchronous database session factory configuration."""

from unittest.mock import Mock

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from maintainer_lens.db.session import create_session_factory


def test_create_session_factory_configuration() -> None:
    """Verify that sessions use the supplied engine and configuration."""
    engine = Mock(spec=AsyncEngine)

    session_factory = create_session_factory(engine)

    assert session_factory.kw["bind"] is engine
    assert session_factory.kw["expire_on_commit"] is False
    assert session_factory.class_ is AsyncSession
