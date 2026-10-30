"""Opt-in paid History-only Worker smoke test with real HTTP, PostgreSQL and MQ.

Requires an existing migrated non-production database/broker and all three flags:
RUN_LIVE_WORKER_GRAPH_SMOKE=1, RUN_POSTGRES_INTEGRATION=1,
RUN_RABBITMQ_INTEGRATION=1. Ordinary tests below only exercise request guards.

This starts at task creation/publication, not POST/Outbox Dispatcher. GET uses the
real route in an in-process ASGI app. One real historical Issue is fixture data,
not a relevance label or a claim that it explains the investigated Issue.
"""

import asyncio
import json
import os
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import aio_pika
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

import maintainer_lens.worker as worker
from maintainer_lens.api.routes.investigations import router
from maintainer_lens.clients.embedding_factory import create_embedding_client
from maintainer_lens.clients.github import fetch_github_issue
from maintainer_lens.clients.github_http import create_github_http_client
from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.investigation_queries import get_investigation_details
from maintainer_lens.db.investigation_results import decode_investigation_result
from maintainer_lens.db.investigation_tasks import create_investigation_task
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.models.investigation_result import (
    INVESTIGATION_RESULT_SCHEMA_VERSION,
)
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.messaging.publisher import publish_investigation
from maintainer_lens.messaging.topology import (
    DEAD_ROUTING_KEY,
    RETRY_ROUTING_KEY,
    ROUTING_KEY,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationResult
from maintainer_lens.services.repository_history_indexing import (
    index_repository_history,
)

pytestmark = pytest.mark.anyio
REPOSITORY = "pypa/sampleproject"
TARGET_ISSUE = 225
HISTORY_ISSUE = 221
REQUIRED_FLAGS = (
    "RUN_LIVE_WORKER_GRAPH_SMOKE",
    "RUN_POSTGRES_INTEGRATION",
    "RUN_RABBITMQ_INTEGRATION",
)
requires_live = pytest.mark.skipif(
    any(os.getenv(name) != "1" for name in REQUIRED_FLAGS),
    reason="Paid Worker smoke requires all three explicit live/DB/broker flags",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _BudgetExceeded(RuntimeError):
    """Refuse an unexpected request before it is sent to a remote provider."""


@dataclass
class _RequestBudget:
    counts: Counter = field(default_factory=Counter)
    statuses: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None

    def charge(self, stage: str) -> None:
        limits = {"github": 4, "embedding": 4, "llm": 10}
        if self.counts[stage] >= limits[stage]:
            raise _BudgetExceeded(f"Live smoke {stage} request limit reached")
        self.counts[stage] += 1

    async def github(self, request: httpx.Request) -> None:
        allowed_paths = {
            f"/repos/{REPOSITORY}",
            f"/repos/{REPOSITORY}/issues/{HISTORY_ISSUE}",
            f"/repos/{REPOSITORY}/issues/{TARGET_ISSUE}",
        }
        if (
            request.method != "GET"
            or request.url.scheme != "https"
            or request.url.host != "api.github.com"
            or request.url.path not in allowed_paths
            or request.url.query
        ):
            raise _BudgetExceeded("Unexpected live smoke GitHub request")
        self.charge("github")

    @staticmethod
    def payload(request: httpx.Request, suffix: str) -> dict:
        if request.method != "POST" or not request.url.path.endswith(suffix):
            raise _BudgetExceeded("Unexpected live smoke provider operation")
        try:
            payload = json.loads(request.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise _BudgetExceeded("Invalid live smoke request JSON") from exc
        if not isinstance(payload, dict):
            raise _BudgetExceeded("Expected a live smoke request object")
        return payload

    async def embedding(self, request: httpx.Request) -> None:
        payload = self.payload(request, "/embeddings")
        inputs = payload.get("input")
        if (
            not isinstance(inputs, list)
            or len(inputs) != 1
            or not isinstance(inputs[0], str)
            or not 1 <= len(inputs[0]) <= 6000
        ):
            raise _BudgetExceeded("Live smoke permits one bounded embedding input")
        self.charge("embedding")

    async def llm(self, request: httpx.Request) -> None:
        payload = self.payload(request, "/chat/completions")
        tokens = payload.get("max_tokens")
        if (
            type(tokens) is not int
            or not 1 <= tokens <= 4096
            or len(request.content) > 32_000
        ):
            raise _BudgetExceeded("Live smoke analysis payload/token limit reached")
        self.charge("llm")

    def response_hook(self, stage):
        async def record(response):
            self.statuses[stage] = response.status_code
            if stage == "llm" and response.is_success:
                await response.aread()
                try:
                    reason = response.json()["choices"][0]["finish_reason"]
                except (ValueError, KeyError, IndexError, TypeError):
                    reason = None
                # Diagnostics never contain headers, credentials or model text.
                known = {"stop", "length", "content_filter", "tool_calls"}
                self.finish_reason = (
                    reason if isinstance(reason, str) and reason in known else "other"
                )

        return record

    def diagnostics(self):
        return {
            "requests": dict(self.counts),
            "statuses": dict(self.statuses),
            "finish_reason": self.finish_reason,
        }


@dataclass
class _OwnedRows:
    task_id: str = field(default_factory=lambda: f"live-graph-{uuid4().hex}")
    task_inserted: bool = False
    seed_fetched_at: datetime | None = None


async def _cleanup_rows(factory, owned):
    """Delete only this task and the unchanged History row inserted by the test."""
    async with factory() as session:
        async with session.begin():
            if owned.task_inserted:
                await session.execute(
                    delete(InvestigationTask).where(
                        InvestigationTask.id == owned.task_id,
                        InvestigationTask.repository == REPOSITORY,
                        InvestigationTask.issue_number == TARGET_ISSUE,
                    )
                )
            if owned.seed_fetched_at is not None:
                # Parent deletion cascades its one embedding. A later external
                # update changes fetched_at and must not be deleted as our data.
                await session.execute(
                    delete(RepositoryHistoryRecord).where(
                        RepositoryHistoryRecord.repository == REPOSITORY,
                        RepositoryHistoryRecord.number == HISTORY_ISSUE,
                        RepositoryHistoryRecord.fetched_at == owned.seed_fetched_at,
                    )
                )


@asynccontextmanager
async def _isolated_broker(settings):
    """Use unique real queues; park retries without a TTL/redelivery loop."""
    async with AsyncExitStack() as stack:
        connection = await create_rabbitmq_connection(
            settings.rabbitmq_url.get_secret_value()
        )
        stack.push_async_callback(connection.close)
        channel = await connection.channel(
            publisher_confirms=True, on_return_raises=True
        )
        stack.push_async_callback(channel.close)
        prefix = f"ml.live.graph.{uuid4().hex}"
        exchange = await channel.declare_exchange(
            prefix,
            type=aio_pika.ExchangeType.DIRECT,
            durable=False,
        )
        stack.push_async_callback(exchange.delete, if_unused=False, timeout=10)
        queues = {}
        for name, routing_key in (
            ("pending", ROUTING_KEY),
            ("retry", RETRY_ROUTING_KEY),
            ("dead", DEAD_ROUTING_KEY),
        ):
            queue = await channel.declare_queue(
                f"{prefix}.{name}",
                durable=False,
                arguments={"x-expires": 300_000},
            )
            stack.push_async_callback(
                queue.delete, if_unused=False, if_empty=False, timeout=10
            )
            await queue.bind(exchange, routing_key=routing_key)
            queues[name] = queue
        yield SimpleNamespace(exchange=exchange, pending=queues["pending"])


class _ObservedDelivery:
    """Observe real committed data before allowing the original delivery ACK."""

    def __init__(self, message, factory, owned, budget, acknowledged):
        self.message = message
        self.factory = factory
        self.owned = owned
        self.budget = budget
        self.acknowledged = acknowledged

    def __getattr__(self, name):
        return getattr(self.message, name)

    async def ack(self):
        async with self.factory() as session:
            details = await get_investigation_details(session, self.owned.task_id)
        status = details[0].status if details is not None else None
        assert status == "succeeded", (
            f"Live Worker did not succeed before ACK: status={status}, "
            f"diagnostics={self.budget.diagnostics()}"
        )
        assert details[1] is not None and details[2] is not None
        assert details[2].schema_version == INVESTIGATION_RESULT_SCHEMA_VERSION
        await self.message.ack()
        self.acknowledged.set()


class _ObservedQueue:
    def __init__(self, queue, factory, owned, budget, acknowledged):
        self.queue = queue
        self.factory = factory
        self.owned = owned
        self.budget = budget
        self.acknowledged = acknowledged

    @asynccontextmanager
    async def iterator(self):
        async with self.queue.iterator() as messages:

            async def observed():
                async for message in messages:
                    yield _ObservedDelivery(
                        message,
                        self.factory,
                        self.owned,
                        self.budget,
                        self.acknowledged,
                    )

            yield observed()


async def _stop_worker(task):
    """The test owns this long-running process task and cancels it after ACK."""
    if not task.done():
        task.cancel()
    async with asyncio.timeout(30):
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    if isinstance(outcome, BaseException) and not isinstance(
        outcome, asyncio.CancelledError
    ):
        raise outcome


async def _wait_for_worker(task, event, seconds):
    waiter = asyncio.create_task(event.wait())
    try:
        async with asyncio.timeout(seconds):
            done, _ = await asyncio.wait(
                {task, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                await task
                raise AssertionError("Live Worker stopped before the expected event")
            await waiter
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def _install_worker_observers(monkeypatch, settings, broker, factory, owned, budget):
    """Change test topology and add counters; keep actual I/O and default callback."""
    clients = []
    provider_stages = iter(("embedding", "llm"))
    ready = asyncio.Event()
    acknowledged = asyncio.Event()
    original_consumer = worker.run_investigation_consumer

    def github_client(token):
        client = create_github_http_client(token)
        client.event_hooks["request"].append(budget.github)
        client.event_hooks["response"].append(budget.response_hook("github"))
        clients.append(client)
        return client

    def provider_client(**kwargs):
        stage = next(provider_stages)
        client = httpx.AsyncClient(**kwargs)
        client.event_hooks["request"].append(getattr(budget, stage))
        client.event_hooks["response"].append(budget.response_hook(stage))
        clients.append(client)
        return client

    async def topology(channel):
        return await channel.get_exchange(broker.exchange.name, ensure=True)

    async def consume(**kwargs):
        kwargs["queue"] = _ObservedQueue(
            kwargs["queue"], factory, owned, budget, acknowledged
        )
        ready.set()
        await original_consumer(**kwargs)

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "QUEUE_NAME", broker.pending.name)
    monkeypatch.setattr(worker, "declare_investigation_topology", topology)
    monkeypatch.setattr(worker, "run_investigation_consumer", consume)
    monkeypatch.setattr(worker, "create_github_http_client", github_client)
    # Patch only this module binding; other httpx clients remain unchanged.
    monkeypatch.setattr(
        worker,
        "httpx",
        SimpleNamespace(AsyncClient=provider_client, Timeout=httpx.Timeout),
    )
    return ready, acknowledged, clients


async def _seed_history(factory, settings, budget, owned, stack):
    github = await stack.enter_async_context(
        create_github_http_client(settings.github_token)
    )
    github.event_hooks["request"].append(budget.github)
    github.event_hooks["response"].append(budget.response_hook("github"))
    embedding_http = await stack.enter_async_context(
        httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            event_hooks={
                "request": [budget.embedding],
                "response": [budget.response_hook("embedding")],
            },
        )
    )
    embedding = create_embedding_client(embedding_http, settings)
    issue = await fetch_github_issue(github, REPOSITORY, HISTORY_ISSUE)
    record = RepositoryHistoryRecord(
        repository=REPOSITORY,
        number=issue.number,
        kind="issue",
        title=issue.title,
        body=issue.body,
        state=issue.state,
        source_url=str(issue.html_url),
        source_created_at=issue.created_at,
        source_updated_at=issue.updated_at,
    )
    async with factory() as session:
        async with session.begin():
            # INSERT deliberately fails on a concurrent/pre-existing key; never
            # upsert over someone else's knowledge for a smoke test.
            session.add(record)
            await session.flush()
            assert record.fetched_at is not None
            owned.seed_fetched_at = record.fetched_at
    indexed = await index_repository_history(
        factory,
        embedding,
        REPOSITORY,
        (HISTORY_ISSUE,),
        embedding_provider_name=worker.WORKER_EMBEDDING_PROVIDER,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        max_comments=0,
        batch_size=1,
    )
    assert indexed.documents_found == indexed.documents_embedded == 1
    assert indexed.documents_reused == 0


async def _assert_readback(factory, owned):
    app = FastAPI()
    app.state.session_factory = factory
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/investigations/{owned.task_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded" and body["issue"] is not None
    result = InvestigationResult.model_validate_json(
        json.dumps(body["result"]), strict=True
    )
    assert result.repository == REPOSITORY and result.issue_number == TARGET_ISSUE
    assert 1 <= result.search_round <= 3 and result.response_language == "en"
    assert result.source_index_id is None and result.source_evidence == []
    assert len(result.history_evidence) == len(result.analysis_evidence) == 1
    assert result.history_evidence[0]["number"] == HISTORY_ISSUE
    context = result.analysis_evidence[0]
    assert context["evidence_id"] == f"history:item:{HISTORY_ISSUE}"
    assert (
        context["source_url"]
        == f"https://github.com/{REPOSITORY}/issues/{HISTORY_ISSUE}"
    )
    assert all(
        set(finding.evidence_ids) <= {context["evidence_id"]}
        for finding in result.preliminary_analysis.findings
    )
    async with factory() as session:
        task, snapshot, record = await get_investigation_details(session, owned.task_id)
    assert task.claim_token is None and task.lease_expires_at is None
    assert (
        snapshot.source_url == f"https://github.com/{REPOSITORY}/issues/{TARGET_ISSUE}"
    )
    saved = decode_investigation_result(
        record,
        task_id=owned.task_id,
        repository=REPOSITORY,
        issue_number=TARGET_ISSUE,
    )
    assert saved.model_dump(mode="json") == result.model_dump(mode="json")
    return result


@requires_live
async def test_live_history_worker_commits_real_result_before_ack(monkeypatch):
    settings = get_settings()  # Only after the explicit opt-in; never print secrets.
    for name in (
        "llm_base_url",
        "llm_model_name",
        "embedding_base_url",
        "embedding_model",
    ):
        value = getattr(settings, name)
        if not value or not value.strip():
            pytest.fail(f"Configure {name.upper()} before enabling the live smoke")
    if (
        settings.llm_api_key is None
        or not settings.llm_api_key.get_secret_value().strip()
    ):
        pytest.fail("Configure LLM_API_KEY before enabling the live smoke")
    assert worker.WORKER_MAX_SEARCH_ROUNDS == 3
    assert worker.WORKER_GRAPH_TIMEOUT_SECONDS == 120.0
    assert worker.WORKER_RESPONSE_LANGUAGE == "en"
    budget = _RequestBudget()
    owned = _OwnedRows()

    async with AsyncExitStack() as stack:
        engine = create_db_engine(settings.database_url.get_secret_value())
        stack.push_async_callback(engine.dispose)
        factory = create_session_factory(engine)
        stack.push_async_callback(_cleanup_rows, factory, owned)
        async with asyncio.timeout(240):
            async with factory() as session:
                existing = await session.scalar(
                    select(RepositoryHistoryRecord.number)
                    .where(
                        RepositoryHistoryRecord.repository == REPOSITORY,
                    )
                    .limit(1)
                )
            if existing is not None:
                pytest.skip(
                    "Live smoke needs an empty sampleproject History corpus; use an isolated test DB"
                )

            broker = await stack.enter_async_context(_isolated_broker(settings))
            await _seed_history(factory, settings, budget, owned, stack)
            async with factory() as session:
                async with session.begin():
                    await create_investigation_task(
                        session,
                        owned.task_id,
                        REPOSITORY,
                        TARGET_ISSUE,
                        response_language="en",
                    )
                    owned.task_inserted = True
            ready, acknowledged, clients = _install_worker_observers(
                monkeypatch, settings, broker, factory, owned, budget
            )
            operation = asyncio.create_task(worker.run_worker())
            stack.push_async_callback(_stop_worker, operation)
            await _wait_for_worker(operation, ready, 30)
            await publish_investigation(
                broker.exchange,
                owned.task_id,
                REPOSITORY,
                TARGET_ISSUE,
                response_language="en",
            )
            await _wait_for_worker(operation, acknowledged, 150)
            await _stop_worker(operation)
            assert len(clients) == 3 and all(client.is_closed for client in clients)
            result = await _assert_readback(factory, owned)
            assert budget.counts["github"] == 4
            assert 2 <= budget.counts["embedding"] <= 4
            assert 1 <= budget.counts["llm"] <= 10
            assert budget.finish_reason in ("stop", "tool_calls")
            print(
                {
                    "repository": REPOSITORY,
                    "issue_number": TARGET_ISSUE,
                    "next_step": result.next_step,
                    "history_evidence": len(result.history_evidence),
                    **budget.diagnostics(),
                }
            )


async def test_request_guards_enforce_paid_call_caps_without_network():
    budget = _RequestBudget()
    embedding = httpx.Request(
        "POST", "https://embedding.example/v1/embeddings", json={"input": ["text"]}
    )
    for _ in range(4):
        await budget.embedding(embedding)
    with pytest.raises(_BudgetExceeded):
        await budget.embedding(embedding)
    chat = httpx.Request(
        "POST", "https://chat.example/v1/chat/completions", json={"max_tokens": 4096}
    )
    for _ in range(10):
        await budget.llm(chat)
    with pytest.raises(_BudgetExceeded):
        await budget.llm(chat)
    github = httpx.Request("GET", f"https://api.github.com/repos/{REPOSITORY}")
    for _ in range(4):
        await budget.github(github)
    with pytest.raises(_BudgetExceeded):
        await budget.github(github)
    assert budget.counts == Counter(github=4, embedding=4, llm=10)


@pytest.mark.parametrize(
    "outbound",
    [
        httpx.Request("POST", "https://api.github.com/repos/pypa/sampleproject/issues"),
        httpx.Request("GET", "https://api.github.com/repos/other/repository"),
        httpx.Request(
            "GET", f"https://api.github.com/repos/{REPOSITORY}/issues?per_page=100"
        ),
    ],
    ids=["github-write", "other-repository", "bulk-history"],
)
async def test_github_guard_rejects_scope_expansion(outbound):
    budget = _RequestBudget()
    with pytest.raises(_BudgetExceeded):
        await budget.github(outbound)
    assert budget.counts == Counter()


@pytest.mark.parametrize("payload", [{"input": ["a", "b"]}, {"input": ["x" * 6001]}])
async def test_embedding_guard_rejects_extra_or_oversized_inputs(payload):
    budget = _RequestBudget()
    request = httpx.Request(
        "POST", "https://embedding.example/embeddings", json=payload
    )
    with pytest.raises(_BudgetExceeded):
        await budget.embedding(request)
    assert budget.counts == Counter()


@pytest.mark.parametrize("tokens", [True, 4097, 0])
async def test_chat_guard_rejects_invalid_token_limits(tokens):
    budget = _RequestBudget()
    request = httpx.Request(
        "POST", "https://chat.example/chat/completions", json={"max_tokens": tokens}
    )
    with pytest.raises(_BudgetExceeded):
        await budget.llm(request)
    assert budget.counts == Counter()


async def test_response_diagnostics_exclude_model_text_and_credentials():
    budget = _RequestBudget()
    response = httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": "length", "message": {"content": "model text"}}
            ],
            "private_debug": "fake-secret-not-for-diagnostics",
        },
    )
    await budget.response_hook("llm")(response)
    assert budget.diagnostics() == {
        "requests": {},
        "statuses": {"llm": 200},
        "finish_reason": "length",
    }
