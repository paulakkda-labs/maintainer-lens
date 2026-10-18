"""FastAPI application initialization and lifecycle management."""

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from maintainer_lens.api.router import router as api_router
from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.clients.redis import create_redis_client
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.messaging.topology import declare_investigation_topology


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application resources during startup and shutdown."""
    settings = get_settings()
    async with AsyncExitStack() as stack:
        engine = create_db_engine(settings.database_url.get_secret_value())
        stack.push_async_callback(engine.dispose)
        app.state.session_factory = create_session_factory(engine)

        redis_client = create_redis_client(settings.redis_url.get_secret_value())
        stack.push_async_callback(redis_client.aclose)
        app.state.redis_client = redis_client

        rabbitmq_connection = await create_rabbitmq_connection(
            settings.rabbitmq_url.get_secret_value()
        )
        stack.push_async_callback(rabbitmq_connection.close)

        rabbitmq_channel = await rabbitmq_connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )
        stack.push_async_callback(rabbitmq_channel.close)
        app.state.rabbitmq_channel = rabbitmq_channel

        rabbitmq_exchange = await declare_investigation_topology(rabbitmq_channel)
        app.state.rabbitmq_exchange = rabbitmq_exchange

        yield


app = FastAPI(title="MaintainerLens", lifespan=lifespan)

app.include_router(api_router)
