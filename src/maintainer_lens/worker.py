"""Entry point and lifecycle management for the investigation worker."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractRobustExchange,
    AbstractRobustQueue,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.github_http import create_github_http_client
from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.messaging.consumer import consume_investigations
from maintainer_lens.messaging.handler import (
    handle_investigation_message_with_retry,
    process_investigation_request,
)
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.messaging.topology import (
    QUEUE_NAME,
    declare_investigation_topology,
)
from maintainer_lens.services.issue_investigator import investigate_issue

logger = logging.getLogger(__name__)


def build_investigation_message_handler(
    session_factory: async_sessionmaker[AsyncSession],
    exchange: AbstractRobustExchange,
    investigate: Callable[
        [InvestigationRequestedMessage, str],
        Awaitable[None],
    ],
) -> Callable[[AbstractIncomingMessage], Awaitable[None]]:
    """Build a message handler with its investigation dependencies."""

    async def process_request(
        request: InvestigationRequestedMessage,
        final_attempt: bool,
    ) -> None:
        await process_investigation_request(
            request=request,
            session_factory=session_factory,
            processor=lambda claim_token: investigate(
                request,
                claim_token,
            ),
            final_attempt=final_attempt,
        )

    async def handle_message(
        message: AbstractIncomingMessage,
    ) -> None:
        await handle_investigation_message_with_retry(
            message=message,
            exchange=exchange,
            processor=process_request,
        )

    return handle_message


async def run_investigation_consumer(
    queue: AbstractRobustQueue,
    exchange: AbstractRobustExchange,
    session_factory: async_sessionmaker[AsyncSession],
    investigate: Callable[
        [InvestigationRequestedMessage],
        Awaitable[None],
    ],
) -> None:
    """Consume investigation messages using the configured processor."""

    handler = build_investigation_message_handler(
        session_factory=session_factory,
        exchange=exchange,
        investigate=investigate,
    )

    await consume_investigations(
        queue=queue,
        handler=handler,
    )


async def run_worker(
    investigate: Callable[
        [InvestigationRequestedMessage, str],
        Awaitable[None],
    ]
    | None = None,
) -> None:
    """Start the worker and manage its investigation dependencies."""

    settings = get_settings()

    async with AsyncExitStack() as stack:
        engine = create_db_engine(settings.database_url.get_secret_value())
        stack.push_async_callback(engine.dispose)

        session_factory = create_session_factory(engine)

        if investigate is None:
            http_client = await stack.enter_async_context(
                create_github_http_client(settings.github_token)
            )

            async def production_investigate(
                request: InvestigationRequestedMessage,
                claim_token: str,
            ) -> None:
                await investigate_issue(
                    http_client=http_client,
                    session_factory=session_factory,
                    task_id=request.task_id,
                    claim_token=claim_token,
                    repository=request.repository,
                    issue_number=request.issue_number,
                )

            investigate = production_investigate

        connection = await create_rabbitmq_connection(
            settings.rabbitmq_url.get_secret_value()
        )
        stack.push_async_callback(connection.close)

        consumer_channel = await connection.channel(
            publisher_confirms=False,
        )
        stack.push_async_callback(consumer_channel.close)

        publisher_channel = await connection.channel(
            publisher_confirms=True,
            on_return_raises=True,
        )
        stack.push_async_callback(publisher_channel.close)

        exchange = await declare_investigation_topology(publisher_channel)

        queue = await consumer_channel.get_queue(
            QUEUE_NAME,
            ensure=True,
        )

        await consumer_channel.set_qos(prefetch_count=1)

        logger.info("Investigation worker initialized successfully")
        logger.info("Waiting for investigation messages")

        await run_investigation_consumer(
            queue=queue,
            exchange=exchange,
            session_factory=session_factory,
            investigate=investigate,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(run_worker())
