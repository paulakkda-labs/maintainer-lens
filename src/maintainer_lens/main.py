"""FastAPI application initialization and lifecycle management."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from maintainer_lens.api.router import router as api_router
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.session import create_session_factory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application resources during startup and shutdown."""
    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())

    try:
        app.state.session_factory = create_session_factory(engine)
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="MaintainerLens", lifespan=lifespan)

app.include_router(api_router)
