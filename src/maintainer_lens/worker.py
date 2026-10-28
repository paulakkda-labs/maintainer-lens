"""Entry point and lifecycle management for the investigation worker."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from uuid import UUID

import httpx
from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractRobustExchange,
    AbstractRobustQueue,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.agents.investigation.execution import with_tool_step_controls
from maintainer_lens.agents.investigation.retrieval import (
    create_history_searcher,
    create_source_searcher,
)
from maintainer_lens.agents.investigation.tool_node import (
    ToolSearchRunner,
    create_tool_search_runner,
)
from maintainer_lens.agents.investigation_analysis import (
    ResponseLanguage,
    create_llm_analyzer,
)
from maintainer_lens.clients.embedding_factory import create_embedding_client
from maintainer_lens.clients.github_http import create_github_http_client
from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.investigation_runner import (
    PermanentInvestigationError,
    RetryableInvestigationError,
)
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
from maintainer_lens.services.investigation_processor import process_graph_investigation
from maintainer_lens.services.investigation_source_index import (
    InvestigationSourceIndexNotReadyError,
    get_investigation_source_index,
)

logger = logging.getLogger(__name__)

# Backend-owned budgets; knowledge preparation remains a separate operation.
WORKER_RESPONSE_LANGUAGE: ResponseLanguage = "zh"
WORKER_MAX_SEARCH_ROUNDS = 3
WORKER_GRAPH_TIMEOUT_SECONDS = 120.0
WORKER_TOOL_TIMEOUT_SECONDS = 60.0
WORKER_EMBEDDING_PROVIDER = "openai-compatible"


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
        [InvestigationRequestedMessage, str],
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
            embedding_http_client = await stack.enter_async_context(
                httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
            )
            embedding_client = create_embedding_client(embedding_http_client, settings)

            llm_http_client = await stack.enter_async_context(
                httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))
            )
            analyzer = create_llm_analyzer(
                llm_http_client,
                base_url=settings.llm_base_url or "",
                api_key=(
                    settings.llm_api_key.get_secret_value()
                    if settings.llm_api_key is not None
                    else ""
                ),
                model_name=settings.llm_model_name or "",
            )
            history_searcher = create_history_searcher(
                session_factory,
                embedding_client,
                embedding_provider_name=WORKER_EMBEDDING_PROVIDER,
                embedding_model=settings.embedding_model or "",
                embedding_dimensions=settings.embedding_dimensions,
                top_k=5,
            )
            source_searcher = create_source_searcher(
                session_factory,
                embedding_client,
                embedding_provider_name=WORKER_EMBEDDING_PROVIDER,
                embedding_model=settings.embedding_model or "",
                embedding_dimensions=settings.embedding_dimensions,
                top_k=5,
            )

            def source_not_ready(
                error: InvestigationSourceIndexNotReadyError,
            ) -> RetryableInvestigationError | PermanentInvestigationError:
                if error.status == "building":
                    return RetryableInvestigationError(
                        "Requested Source index is building"
                    )
                return PermanentInvestigationError(
                    "Requested Source index is not usable"
                )

            async def validate_source_index(repository: str, index_id: UUID) -> None:
                try:
                    async with session_factory() as session, session.begin():
                        await get_investigation_source_index(
                            session,
                            repository=repository,
                            source_index_id=index_id,
                            embedding_provider_name=WORKER_EMBEDDING_PROVIDER,
                            embedding_model=settings.embedding_model or "",
                            embedding_dimensions=settings.embedding_dimensions,
                        )
                except InvestigationSourceIndexNotReadyError as exc:
                    raise source_not_ready(exc) from exc

            async def production_source_search(
                repository: str, index_id: UUID, query: str
            ):
                try:
                    return await source_searcher(repository, index_id, query)
                except InvestigationSourceIndexNotReadyError as exc:
                    raise source_not_ready(exc) from exc

            def tool_runner(*, enable_source: bool) -> ToolSearchRunner:
                return with_tool_step_controls(
                    create_tool_search_runner(
                        llm_http_client,
                        base_url=settings.llm_base_url or "",
                        api_key=settings.llm_api_key.get_secret_value(),
                        model_name=settings.llm_model_name or "",
                        history_searcher=history_searcher,
                        source_searcher=production_source_search
                        if enable_source
                        else None,
                    ),
                    timeout_seconds=WORKER_TOOL_TIMEOUT_SECONDS,
                )

            history_tool_runner = tool_runner(enable_source=False)
            source_tool_runner = tool_runner(enable_source=True)

            async def production_investigate(
                request: InvestigationRequestedMessage,
                claim_token: str,
            ) -> None:
                await process_graph_investigation(
                    http_client=http_client,
                    session_factory=session_factory,
                    task_id=request.task_id,
                    claim_token=claim_token,
                    repository=request.repository,
                    issue_number=request.issue_number,
                    response_language=WORKER_RESPONSE_LANGUAGE,
                    history_searcher=history_searcher,
                    analyzer=analyzer,
                    timeout_seconds=WORKER_GRAPH_TIMEOUT_SECONDS,
                    max_search_rounds=WORKER_MAX_SEARCH_ROUNDS,
                    source_index_id=request.source_index_id,
                    source_searcher=(
                        production_source_search
                        if request.source_index_id is not None
                        else None
                    ),
                    source_index_validator=validate_source_index,
                    tool_search_runner=(
                        source_tool_runner
                        if request.source_index_id is not None
                        else history_tool_runner
                    ),
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
