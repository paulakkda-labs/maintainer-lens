"""Run the independent investigation outbox dispatcher process."""

import asyncio
import logging
from contextlib import AsyncExitStack

from aio_pika.abc import AbstractRobustExchange
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.messaging.topology import declare_investigation_topology
from maintainer_lens.services.outbox_dispatcher import (
    dispatch_one_outbox_event,
)

logger = logging.getLogger(__name__)

IDLE_POLL_INTERVAL_SECONDS = 1.0
ERROR_RETRY_INTERVAL_SECONDS = 5.0


async def run_dispatcher_loop(
    session_factory: async_sessionmaker[AsyncSession],
    exchange: AbstractRobustExchange,
) -> None:
    """Continuously deliver pending outbox events."""

    while True:
        try:
            dispatched = await dispatch_one_outbox_event(
                session_factory,
                exchange,
            )

        except Exception:
            logger.exception("Outbox dispatch attempt failed")

            await asyncio.sleep(ERROR_RETRY_INTERVAL_SECONDS)
            continue

        if not dispatched:
            await asyncio.sleep(IDLE_POLL_INTERVAL_SECONDS)


async def run_outbox_dispatcher() -> None:
    """Initialize dispatcher resources and start its delivery loop."""

    settings = get_settings()

    async with AsyncExitStack() as stack:
        engine = create_db_engine(settings.database_url.get_secret_value())
        stack.push_async_callback(engine.dispose)

        session_factory = create_session_factory(engine)

        connection = await create_rabbitmq_connection(
            settings.rabbitmq_url.get_secret_value()
        )
        stack.push_async_callback(connection.close)

        channel = await connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )
        stack.push_async_callback(channel.close)

        exchange = await declare_investigation_topology(channel)

        logger.info("Outbox dispatcher started")

        await run_dispatcher_loop(
            session_factory=session_factory,
            exchange=exchange,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    try:
        asyncio.run(run_outbox_dispatcher())
    except KeyboardInterrupt:
        pass
