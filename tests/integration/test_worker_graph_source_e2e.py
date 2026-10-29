"""POST/Outbox/default Worker/GET with real PostgreSQL and isolated RabbitMQ.

Fake-provider cases require RUN_POSTGRES_INTEGRATION=1 and
RUN_RABBITMQ_INTEGRATION=1. The separate paid case additionally requires
RUN_LIVE_SOURCE_WORKER_SMOKE=1. It prepares one verified public Source chunk
and one historical Issue itself, then removes its owned fixture data. No manual
UUID, full repository build, migrations or GitHub writes are needed. Default
tests exercise only offline request guards.
"""

import asyncio
import base64
import json
import os
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha1, sha256
from types import SimpleNamespace
from uuid import UUID, uuid4

import aio_pika
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update

import maintainer_lens.services.investigation_submission as submission
import maintainer_lens.services.outbox_dispatcher as dispatcher
import maintainer_lens.worker as worker
from maintainer_lens.api.routes.investigations import router
from maintainer_lens.clients.embedding_factory import create_embedding_client
from maintainer_lens.clients.github import fetch_github_issue
from maintainer_lens.clients.github_blob import fetch_repository_blob
from maintainer_lens.clients.github_http import create_github_http_client
from maintainer_lens.clients.github_repository import resolve_repository_revision
from maintainer_lens.clients.github_tree import fetch_repository_manifest
from maintainer_lens.clients.rabbitmq import create_rabbitmq_connection
from maintainer_lens.core.config import Settings, get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.investigation_queries import get_investigation_details
from maintainer_lens.db.models.investigation import (
    InvestigationOutboxEvent,
    InvestigationTask,
)
from maintainer_lens.db.models.investigation_checkpoint import (
    InvestigationCheckpoint,
    InvestigationGraphRun,
)
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.db.models.repository_history_embedding import (
    RepositoryHistoryEmbedding,
)
from maintainer_lens.db.models.repository_index import RepositoryChunk, RepositoryIndex
from maintainer_lens.db.session import create_session_factory
from maintainer_lens.messaging.publisher import publish_investigation
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.messaging.topology import (
    DEAD_ROUTING_KEY,
    RETRY_ROUTING_KEY,
    ROUTING_KEY,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationResult
from maintainer_lens.schemas.repository import RepositorySource
from maintainer_lens.services.investigation_preparation import (
    AUTO_SOURCE_CHUNKER_VERSION,
)
from maintainer_lens.services.investigation_report import render_investigation_report
from maintainer_lens.services.investigation_source_index import (
    get_investigation_source_index,
)
from maintainer_lens.services.repository_history_indexing import (
    index_repository_history,
)
from maintainer_lens.services.repository_index_writer import write_repository_index
from maintainer_lens.services.repository_indexing import embed_repository_chunks
from maintainer_lens.services.source_chunker import chunk_repository_source

pytestmark = pytest.mark.anyio
infrastructure_enabled = all(
    os.getenv(name) == "1"
    for name in ("RUN_POSTGRES_INTEGRATION", "RUN_RABBITMQ_INTEGRATION")
)
requires_infrastructure = pytest.mark.skipif(
    not infrastructure_enabled,
    reason="Full Graph E2E requires explicit PostgreSQL and RabbitMQ flags",
)
requires_live = pytest.mark.skipif(
    not infrastructure_enabled or os.getenv("RUN_LIVE_SOURCE_WORKER_SMOKE") != "1",
    reason="Paid Source Worker smoke requires explicit live/DB/broker flags",
)
LIVE_REPOSITORY = "pypa/sampleproject"
TARGET_ISSUE = 225
HISTORY_ISSUE = 221


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _BudgetExceeded(RuntimeError):
    """A smoke request exceeded its authorized scope or budget before sending."""


@dataclass
class _Budget:
    counts: Counter = field(default_factory=Counter)
    limits: dict = field(
        default_factory=lambda: {"github": 8, "embedding": 6, "llm": 7}
    )
    statuses: dict = field(default_factory=dict)
    finish_reasons: list = field(default_factory=list)
    completion_metrics: list = field(default_factory=list)
    source_tree_sha: str | None = None
    source_blob_sha: str | None = None
    source_requests: Counter = field(default_factory=Counter)

    async def github(self, request):
        prefix = f"/repos/{LIVE_REPOSITORY}"
        paths = {
            prefix,
            f"{prefix}/issues/{TARGET_ISSUE}",
            f"{prefix}/issues/{HISTORY_ISSUE}",
        }
        path = request.url.path
        operation = None
        allowed = path in paths and not request.url.query
        branch_prefix = f"{prefix}/branches/"
        if (
            path.startswith(branch_prefix)
            and path != branch_prefix
            and not request.url.query
        ):
            operation, allowed = "branch", True
        if (
            self.source_tree_sha
            and path == f"{prefix}/git/trees/{self.source_tree_sha}"
            and request.url.query == b"recursive=1"
        ):
            operation, allowed = "tree", True
        if (
            self.source_blob_sha
            and path == f"{prefix}/git/blobs/{self.source_blob_sha}"
            and not request.url.query
        ):
            operation, allowed = "blob", True
        if (
            request.method != "GET"
            or request.url.scheme != "https"
            or request.url.host != "api.github.com"
            or not allowed
        ):
            raise _BudgetExceeded("GitHub request is outside the smoke scope")
        if operation and self.source_requests[operation] >= 1:
            raise _BudgetExceeded(
                "Smoke permits one branch, tree and blob request each"
            )
        self.charge("github")
        if operation:
            self.source_requests[operation] += 1

    def charge(self, stage):
        if self.counts[stage] >= self.limits[stage]:
            raise _BudgetExceeded(f"Smoke {stage} request cap reached")
        self.counts[stage] += 1

    async def embedding(self, request):
        payload = self.payload(request, "/embeddings")
        inputs = payload.get("input")
        if (
            not isinstance(inputs, list)
            or len(inputs) != 1
            or not isinstance(inputs[0], str)
            or not 1 <= len(inputs[0]) <= 6000
        ):
            raise _BudgetExceeded("Smoke permits one bounded embedding input")
        self.charge("embedding")

    async def llm(self, request):
        payload = self.payload(request, "/chat/completions")
        tokens = payload.get("max_tokens")
        if (
            type(tokens) is not int
            or not 1 <= tokens <= 4096
            or len(request.content) > 64_000
        ):
            raise _BudgetExceeded("Smoke chat payload/token limit reached")
        self.charge("llm")

    @staticmethod
    def payload(request, suffix):
        if request.method != "POST" or not request.url.path.endswith(suffix):
            raise _BudgetExceeded("Unexpected provider operation")
        try:
            value = json.loads(request.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise _BudgetExceeded("Invalid provider JSON") from exc
        if not isinstance(value, dict):
            raise _BudgetExceeded("Expected provider request object")
        return value

    def response(self, stage):
        async def record(response):
            self.statuses[stage] = response.status_code
            if stage == "llm" and response.is_success:
                await response.aread()
                payload, choice = {}, {}
                try:
                    payload = response.json()
                    choice = payload["choices"][0]
                    reason = choice["finish_reason"]
                except (ValueError, KeyError, IndexError, TypeError):
                    reason = "other"
                self.finish_reasons.append(
                    reason
                    if reason in ("stop", "tool_calls", "length", "content_filter")
                    else "other"
                )
                message = choice.get("message") if isinstance(choice, dict) else None
                metrics = {}
                for field_name in ("content", "reasoning_content"):
                    value = (
                        message.get(field_name) if isinstance(message, dict) else None
                    )
                    metrics[f"{field_name}_characters"] = (
                        len(value) if isinstance(value, str) else 0
                    )
                usage = payload.get("usage") if isinstance(payload, dict) else None
                if isinstance(usage, dict):
                    for field_name in (
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                    ):
                        value = usage.get(field_name)
                        if type(value) is int and value >= 0:
                            metrics[field_name] = value
                    details = usage.get("completion_tokens_details")
                    reasoning_tokens = (
                        details.get("reasoning_tokens")
                        if isinstance(details, dict)
                        else None
                    )
                    if type(reasoning_tokens) is int and reasoning_tokens >= 0:
                        metrics["reasoning_tokens"] = reasoning_tokens
                self.completion_metrics.append(metrics)

        return record

    def diagnostics(self):
        return {
            "requests": dict(self.counts),
            "statuses": dict(self.statuses),
            "finish_reasons": list(self.finish_reasons),
            "completion_metrics": list(self.completion_metrics),
        }


@asynccontextmanager
async def _broker(settings):
    async with AsyncExitStack() as stack:
        connection = await create_rabbitmq_connection(
            settings.rabbitmq_url.get_secret_value()
        )
        stack.push_async_callback(connection.close)
        channel = await connection.channel(
            publisher_confirms=True, on_return_raises=True
        )
        stack.push_async_callback(channel.close)
        prefix = f"ml.source.e2e.{uuid4().hex}"
        exchange = await channel.declare_exchange(
            prefix, type=aio_pika.ExchangeType.DIRECT, durable=False
        )
        stack.push_async_callback(exchange.delete, if_unused=False, timeout=10)
        queues = {}
        for name, key in (
            ("pending", ROUTING_KEY),
            ("retry", RETRY_ROUTING_KEY),
            ("dead", DEAD_ROUTING_KEY),
        ):
            queue = await channel.declare_queue(
                f"{prefix}.{name}", durable=False, arguments={"x-expires": 300_000}
            )
            stack.push_async_callback(
                queue.delete, if_unused=False, if_empty=False, timeout=10
            )
            await queue.bind(exchange, routing_key=key)
            queues[name] = queue
        yield SimpleNamespace(exchange=exchange, **queues)


@dataclass
class _Watch:
    factory: object
    task_id: str
    expected_status: str = "succeeded"
    budget: _Budget | None = None
    snapshot_on_failure: bool = False
    acknowledgements: int = 0
    event: asyncio.Event = field(default_factory=asyncio.Event)

    async def before_ack(self, message):
        async with self.factory() as session:
            task, snapshot, record = await get_investigation_details(
                session, self.task_id
            )
        assert task.status == self.expected_status, (
            task.status,
            self.budget.diagnostics() if self.budget else {},
        )
        if self.expected_status == "succeeded":
            assert snapshot is not None and record is not None
            assert task.claim_token is None and task.lease_expires_at is None
            if task.auto_prepare_knowledge:
                assert task.prepared_source_index_id is not None
                assert record.result_payload["source_index_id"] == str(
                    task.prepared_source_index_id
                )
        else:
            assert (snapshot is not None) == self.snapshot_on_failure and record is None
        await message.ack()
        self.acknowledgements += 1
        self.event.set()


class _Delivery:
    def __init__(self, message, watch):
        self.message, self.watch = message, watch

    def __getattr__(self, name):
        return getattr(self.message, name)

    async def ack(self):
        await self.watch.before_ack(self.message)


class _Queue:
    def __init__(self, queue, watch):
        self.queue, self.watch = queue, watch

    @asynccontextmanager
    async def iterator(self):
        async with self.queue.iterator() as stream:

            async def observed():
                async for message in stream:
                    yield _Delivery(message, self.watch)

            yield observed()


async def _stop(operation):
    if not operation.done():
        operation.cancel()
    async with asyncio.timeout(30):
        outcome = (await asyncio.gather(operation, return_exceptions=True))[0]
    if isinstance(outcome, BaseException) and not isinstance(
        outcome, asyncio.CancelledError
    ):
        raise outcome


async def _wait(operation, event, *, timeout_seconds=150):
    waiter = asyncio.create_task(event.wait())
    try:
        async with asyncio.timeout(timeout_seconds):
            done, _ = await asyncio.wait(
                {operation, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if operation in done:
                await operation
                raise AssertionError("Worker stopped before acknowledgement")
            await waiter
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def _observe_worker(
    monkeypatch, settings, broker, watch, *, transport=None, budget=None
):
    original_consume = worker.run_investigation_consumer
    ready = asyncio.Event()
    clients = []
    stages = iter(("embedding", "llm"))

    def github_client(token):
        client = (
            create_github_http_client(token)
            if transport is None
            else httpx.AsyncClient(
                base_url="https://api.github.com", transport=transport, timeout=15
            )
        )
        if budget:
            client.event_hooks["request"].append(budget.github)
            client.event_hooks["response"].append(budget.response("github"))
        clients.append(client)
        return client

    def provider_client(**kwargs):
        stage = next(stages)
        client = httpx.AsyncClient(transport=transport, **kwargs)
        if budget:
            client.event_hooks["request"].append(getattr(budget, stage))
            client.event_hooks["response"].append(budget.response(stage))
        clients.append(client)
        return client

    async def topology(channel):
        return await channel.get_exchange(broker.exchange.name, ensure=True)

    async def consume(**kwargs):
        kwargs["queue"] = _Queue(kwargs["queue"], watch)
        ready.set()
        await original_consume(**kwargs)

    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "QUEUE_NAME", broker.pending.name)
    monkeypatch.setattr(worker, "declare_investigation_topology", topology)
    monkeypatch.setattr(worker, "run_investigation_consumer", consume)
    monkeypatch.setattr(worker, "create_github_http_client", github_client)
    monkeypatch.setattr(
        worker,
        "httpx",
        SimpleNamespace(AsyncClient=provider_client, Timeout=httpx.Timeout),
    )
    return ready, clients


class _Providers:
    def __init__(self, repository, *, source):
        self.repository, self.source = repository, source
        self.requests = Counter()

    async def __call__(self, request):
        if request.url.host == "api.github.com":
            self.requests["github"] += 1
            if request.url.path == f"/repos/{self.repository}":
                return httpx.Response(200, json={"private": False})
            assert request.url.path == f"/repos/{self.repository}/issues/123"
            return httpx.Response(
                200,
                json={
                    "number": 123,
                    "title": "Connection cleanup",
                    "body": None,
                    "state": "open",
                    "html_url": f"https://github.com/{self.repository}/issues/123",
                    "created_at": "2026-10-01T00:00:00Z",
                    "updated_at": "2026-10-28T00:00:00Z",
                },
            )
        payload = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            self.requests["embedding"] += 1
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}
            )
        assert request.url.path.endswith("/chat/completions")
        self.requests["llm"] += 1
        if payload.get("tool_choice") == "required":
            tool = "search_source" if self.source else "search_history"
            allowed = [item["function"]["name"] for item in payload["tools"]]
            assert allowed == (
                ["search_history", "search_source"]
                if self.source
                else ["search_history"]
            )
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "e2e-call",
                        "type": "function",
                        "function": {
                            "name": tool,
                            "arguments": json.dumps(
                                {"query": "followup cleanup details"}
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        elif payload.get("tool_choice") == "none":
            assert payload["messages"][-1]["tool_call_id"] == "e2e-call"
            message, finish = (
                {"content": "Free-text feedback is not a finding."},
                "stop",
            )
        else:
            context = json.loads(payload["messages"][1]["content"])
            assert {item["category"] for item in context["evidence"]} == (
                {"history", "source"} if self.source else {"history"}
            )
            message, finish = (
                {
                    "content": json.dumps(
                        {
                            "summary": "找到相关证据，仍需调查。",
                            "findings": [
                                {
                                    "statement": "证据包含清理实现或讨论。",
                                    "evidence_ids": [
                                        item["evidence_id"]
                                        for item in context["evidence"]
                                    ],
                                }
                            ],
                            "hypotheses": [],
                            "unanswered_questions": ["需要确认实际执行路径。"],
                        }
                    )
                },
                "stop",
            )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": finish, "message": message}]}
        )


def _fake_settings(settings):
    return Settings(
        _env_file=None,
        database_url=settings.database_url,
        redis_url="redis://127.0.0.1:6379/0",
        rabbitmq_url=settings.rabbitmq_url,
        llm_base_url="https://chat.example/v1",
        llm_model_name="e2e-analysis",
        llm_api_key="fake-key",
        embedding_base_url="https://embedding.example/v1",
        embedding_model="e2e-embedding",
        embedding_api_key="fake-key",
        embedding_dimensions=3,
        embedding_request_dimensions=None,
    )


async def _seed_fake(factory, repository, index_id, *, invalid=False):
    now = datetime(2026, 10, 28, tzinfo=UTC)
    async with factory() as session, session.begin():
        session.add(
            RepositoryHistoryRecord(
                repository=repository,
                number=124,
                kind="issue",
                title="Cleanup history",
                body="Cleanup discussion.",
                state="open",
                source_url=f"https://github.com/{repository}/issues/124",
                source_created_at=now,
                source_updated_at=now,
            )
        )
        await session.flush()
        session.add(
            RepositoryHistoryEmbedding(
                repository=repository,
                source_key="item:124",
                source_type="item",
                number=124,
                comment_id=None,
                title="Cleanup history",
                content="Cleanup discussion.",
                source_url=f"https://github.com/{repository}/issues/124",
                source_updated_at=now,
                embedding_provider="openai-compatible",
                embedding_model="e2e-embedding",
                embedding_dimensions=3,
                content_sha256=sha256(b"Cleanup discussion.").hexdigest(),
                embedding=[1.0, 0.0, 0.0],
            )
        )
        if index_id:
            session.add(
                RepositoryIndex(
                    id=index_id,
                    repository=repository,
                    commit_sha="a" * 40,
                    tree_sha="b" * 40,
                    embedding_provider="openai-compatible",
                    embedding_model="e2e-embedding",
                    embedding_dimensions=4 if invalid else 3,
                    chunker_version="source-e2e",
                    status="ready",
                )
            )
            await session.flush()
            session.add(
                RepositoryChunk(
                    index_id=index_id,
                    chunk_key="c" * 64,
                    file_path="client.py",
                    blob_sha="b" * 40,
                    language="python",
                    kind="function",
                    symbol="cleanup",
                    start_line=1,
                    end_line=2,
                    content="def cleanup():\n    return True",
                    embedding=[1.0, 0.0, 0.0],
                )
            )


async def _cleanup(
    factory,
    task_id,
    repository,
    *,
    index_id=None,
    history_fetched_at=None,
    fake=False,
    source_chunker_version=None,
):
    async with factory() as session, session.begin():
        await session.execute(
            delete(InvestigationTask).where(InvestigationTask.id == task_id)
        )
        if fake or history_fetched_at is not None:
            condition = [RepositoryHistoryRecord.repository == repository]
            if not fake:
                condition.extend(
                    [
                        RepositoryHistoryRecord.number == HISTORY_ISSUE,
                        RepositoryHistoryRecord.fetched_at == history_fetched_at,
                    ]
                )
            await session.execute(delete(RepositoryHistoryRecord).where(*condition))
        if fake and index_id:
            await session.execute(
                delete(RepositoryIndex).where(RepositoryIndex.id == index_id)
            )
        if source_chunker_version is not None:
            # The unique fixture marker also covers commit-before-return cancellation.
            await session.execute(
                delete(RepositoryIndex).where(
                    RepositoryIndex.repository == repository,
                    RepositoryIndex.chunker_version == source_chunker_version,
                )
            )


async def _post_dispatch_read(
    monkeypatch,
    settings,
    factory,
    broker,
    watch,
    repository,
    issue_number,
    source_index_id,
    *,
    transport=None,
    budget=None,
    automatic=False,
):
    app = FastAPI()
    app.state.session_factory = factory
    app.include_router(router)
    generated_id = UUID(watch.task_id)
    monkeypatch.setattr(submission, "uuid4", lambda: generated_id)
    original_claim = dispatcher.claim_next_outbox_event

    async def scoped_claim(session):
        # Real claim/lease SQL, but reject a foreign task inside the same transaction.
        # Rollback prevents publishing or durably claiming another developer's event.
        claimed = await original_claim(session)
        if claimed is not None and claimed.task_id != watch.task_id:
            raise AssertionError(
                "E2E needs an isolated Outbox; another event would be dispatched"
            )
        return claimed

    monkeypatch.setattr(dispatcher, "claim_next_outbox_event", scoped_claim)
    ready, clients = _observe_worker(
        monkeypatch, settings, broker, watch, transport=transport, budget=budget
    )
    operation = asyncio.create_task(worker.run_worker())
    try:
        await _wait(operation, ready)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/investigations/from-url" if automatic else "/investigations",
                json={
                    "issue_url": f"https://github.com/{repository}/issues/{issue_number}",
                    "knowledge_budget_profile": "small",
                }
                if automatic
                else {
                    "repository": repository,
                    "issue_number": issue_number,
                    "source_index_id": str(source_index_id)
                    if source_index_id
                    else None,
                },
            )
            assert (
                response.status_code == 202
                and response.json()["task_id"] == watch.task_id
            )
            async with factory() as session:
                task = await session.get(InvestigationTask, watch.task_id)
                event = await session.scalar(
                    select(InvestigationOutboxEvent).where(
                        InvestigationOutboxEvent.task_id == watch.task_id
                    )
                )
            assert task.auto_prepare_knowledge == automatic
            assert task.source_index_id == source_index_id
            assert event.payload["auto_prepare_knowledge"] == automatic
            assert event.payload["source_index_id"] == (
                str(source_index_id) if source_index_id else None
            )
            assert event.published_at is None
            assert await dispatcher.dispatch_one_outbox_event(factory, broker.exchange)
            await _wait(
                operation, watch.event, timeout_seconds=450 if automatic else 150
            )
            detail = await client.get(f"/investigations/{watch.task_id}")
            assert detail.status_code == 200
            data = detail.json()
            assert data["status"] == watch.expected_status
            assert data["source_index_id"] == (
                str(source_index_id) if source_index_id else None
            )
            async with factory() as session:
                event = await session.scalar(
                    select(InvestigationOutboxEvent).where(
                        InvestigationOutboxEvent.task_id == watch.task_id
                    )
                )
            assert event.published_at is not None and event.delivery_token is None
            assert event.publish_attempts == 1
            if watch.expected_status == "succeeded":
                result = InvestigationResult.model_validate_json(
                    json.dumps(data["result"]), strict=True
                )
                effective_index = (
                    UUID(data["prepared_source_index_id"])
                    if automatic
                    else source_index_id
                )
                assert result.source_index_id == effective_index
                assert data["auto_prepare_knowledge"] == automatic
                assert data["report_markdown"] == render_investigation_report(result)
                assert result.history_evidence and result.analysis_evidence
                assert bool(result.source_evidence) == (effective_index is not None)
                watch.event.clear()
                await publish_investigation(
                    broker.exchange,
                    watch.task_id,
                    repository,
                    issue_number,
                    source_index_id,
                    auto_prepare_knowledge=automatic,
                )
                await _wait(operation, watch.event)
                assert watch.acknowledgements == 2
                assert (
                    await client.get(f"/investigations/{watch.task_id}")
                ).json() == data
            else:
                assert data["result"] is None and data["report_markdown"] is None
                dead = await broker.dead.get(timeout=5, fail=True)
                assert (
                    InvestigationRequestedMessage.model_validate_json(
                        dead.body
                    ).source_index_id
                    == source_index_id
                )
                await dead.ack()
            return data
    finally:
        await _stop(operation)
        assert len(clients) == 3 and all(client.is_closed for client in clients)


@requires_infrastructure
@pytest.mark.parametrize("mode", ["history", "source", "invalid_source"])
async def test_complete_post_outbox_worker_graph_get_with_fake_providers(
    monkeypatch, mode
):
    settings = _fake_settings(get_settings())
    repository = f"graph-e2e/repo-{uuid4().hex}"
    task_id = str(uuid4())
    index_id = uuid4() if mode != "history" else None
    providers = _Providers(repository, source=index_id is not None)
    engine = create_db_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    try:
        await _seed_fake(
            factory, repository, index_id, invalid=mode == "invalid_source"
        )
        async with _broker(settings) as broker:
            watch = _Watch(
                factory,
                task_id,
                expected_status="failed" if mode == "invalid_source" else "succeeded",
            )
            data = await _post_dispatch_read(
                monkeypatch,
                settings,
                factory,
                broker,
                watch,
                repository,
                123,
                index_id,
                transport=httpx.MockTransport(providers),
            )
        if mode == "invalid_source":
            assert providers.requests == Counter()
        else:
            assert data["result"]["search_round"] == 2
            assert data["result"]["next_step"] == "needs_more_evidence"
            assert providers.requests == Counter(
                github=2, embedding=3 if index_id else 2, llm=3
            )
    finally:
        try:
            await _cleanup(factory, task_id, repository, index_id=index_id, fake=True)
        finally:
            await engine.dispose()


async def _seed_live_source(
    factory, github, embedding, settings, budget, chunker_version
):
    """Prepare one verified real chunk, with a unique test-owned index identity."""
    revision = await resolve_repository_revision(github, LIVE_REPOSITORY)
    budget.source_tree_sha = revision.tree_sha
    manifest = await fetch_repository_manifest(github, revision)
    candidates = sorted(
        (
            file
            for file in manifest.files
            if file.path.endswith(".py") and 0 < file.size <= 4000
        ),
        key=lambda file: (file.size, file.path),
    )
    assert candidates, "The smoke needs one small allowlisted Python file"
    selected = candidates[0]
    budget.source_blob_sha = selected.blob_sha
    content = await fetch_repository_blob(github, LIVE_REPOSITORY, selected)
    chunks = chunk_repository_source(RepositorySource(file=selected, content=content))
    assert chunks, "The selected verified file must produce a source chunk"
    # A deliberately partial fixture, not a complete repository index build.
    embedded = await embed_repository_chunks(embedding, chunks[:1], batch_size=1)
    async with factory() as session:
        return await write_repository_index(
            session,
            revision,
            embedded,
            embedding_provider=worker.WORKER_EMBEDDING_PROVIDER,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_dimensions,
            chunker_version=chunker_version,
        )


@requires_live
async def test_paid_source_history_post_outbox_worker_smoke(monkeypatch):
    settings = get_settings()
    for name in (
        "llm_base_url",
        "llm_model_name",
        "embedding_base_url",
        "embedding_model",
    ):
        if not getattr(settings, name) or not getattr(settings, name).strip():
            pytest.fail(f"Configure {name.upper()} before enabling the paid smoke")
    if (
        settings.llm_api_key is None
        or not settings.llm_api_key.get_secret_value().strip()
    ):
        pytest.fail("Configure LLM_API_KEY before enabling the paid smoke")
    budget = _Budget()
    task_id = str(uuid4())
    owned = SimpleNamespace(
        history_fetched_at=None, source_chunker_version=f"smoke-{uuid4().hex[:24]}"
    )
    async with AsyncExitStack() as stack:
        engine = create_db_engine(settings.database_url.get_secret_value())
        stack.push_async_callback(engine.dispose)
        factory = create_session_factory(engine)

        async def cleanup():
            await _cleanup(
                factory,
                task_id,
                LIVE_REPOSITORY,
                history_fetched_at=owned.history_fetched_at,
                source_chunker_version=owned.source_chunker_version,
            )

        stack.push_async_callback(cleanup)
        async with asyncio.timeout(300):
            async with factory() as session, session.begin():
                existing = await session.scalar(
                    select(RepositoryHistoryRecord.number)
                    .where(RepositoryHistoryRecord.repository == LIVE_REPOSITORY)
                    .limit(1)
                )
            if existing is not None:
                pytest.skip(
                    "Use an isolated DB without sampleproject History; existing knowledge is preserved"
                )
            github = await stack.enter_async_context(
                create_github_http_client(settings.github_token)
            )
            github.event_hooks["request"].append(budget.github)
            github.event_hooks["response"].append(budget.response("github"))
            embedding_http = await stack.enter_async_context(
                httpx.AsyncClient(
                    timeout=60,
                    event_hooks={
                        "request": [budget.embedding],
                        "response": [budget.response("embedding")],
                    },
                )
            )
            embedding = create_embedding_client(embedding_http, settings)
            index_id = await _seed_live_source(
                factory,
                github,
                embedding,
                settings,
                budget,
                owned.source_chunker_version,
            )
            async with factory() as session, session.begin():
                await get_investigation_source_index(
                    session,
                    repository=LIVE_REPOSITORY,
                    source_index_id=index_id,
                    embedding_provider_name=worker.WORKER_EMBEDDING_PROVIDER,
                    embedding_model=settings.embedding_model,
                    embedding_dimensions=settings.embedding_dimensions,
                )
            issue = await fetch_github_issue(github, LIVE_REPOSITORY, HISTORY_ISSUE)
            async with factory() as session, session.begin():
                record = RepositoryHistoryRecord(
                    repository=LIVE_REPOSITORY,
                    number=issue.number,
                    kind="issue",
                    title=issue.title,
                    body=issue.body,
                    state=issue.state,
                    source_url=str(issue.html_url),
                    source_created_at=issue.created_at,
                    source_updated_at=issue.updated_at,
                )
                session.add(record)
                await session.flush()
                owned.history_fetched_at = record.fetched_at
            indexed = await index_repository_history(
                factory,
                embedding,
                LIVE_REPOSITORY,
                (HISTORY_ISSUE,),
                embedding_provider_name=worker.WORKER_EMBEDDING_PROVIDER,
                embedding_model=settings.embedding_model,
                embedding_dimensions=settings.embedding_dimensions,
                max_comments=0,
                batch_size=1,
            )
            assert indexed.documents_embedded == 1
            async with _broker(settings) as broker:
                watch = _Watch(factory, task_id, budget=budget)
                data = await _post_dispatch_read(
                    monkeypatch,
                    settings,
                    factory,
                    broker,
                    watch,
                    LIVE_REPOSITORY,
                    TARGET_ISSUE,
                    index_id,
                    budget=budget,
                )
            assert 1 <= data["result"]["search_round"] <= 3
            assert budget.counts["github"] == 8
            assert 4 <= budget.counts["embedding"] <= 6
            assert 1 <= budget.counts["llm"] <= 7
            assert "stop" in budget.finish_reasons
            assert all(
                reason in ("stop", "tool_calls") for reason in budget.finish_reasons
            )
            print(
                {
                    "repository": LIVE_REPOSITORY,
                    "issue_number": TARGET_ISSUE,
                    "source_index_id": str(index_id),
                    "next_step": data["result"]["next_step"],
                    "search_round": data["result"]["search_round"],
                    **budget.diagnostics(),
                }
            )


@pytest.mark.parametrize(
    "stage,suffix,payload",
    [
        ("embedding", "/embeddings", {"input": ["text"]}),
        ("llm", "/chat/completions", {"max_tokens": 4096}),
    ],
)
async def test_paid_source_request_caps_are_enforced_offline(stage, suffix, payload):
    budget = _Budget()
    request = httpx.Request("POST", f"https://provider.example{suffix}", json=payload)
    guard = getattr(budget, stage)
    for _ in range(budget.limits[stage]):
        await guard(request)
    with pytest.raises(_BudgetExceeded):
        await guard(request)
    assert budget.counts[stage] == budget.limits[stage]


@pytest.mark.parametrize(
    "stage,outbound",
    [
        ("github", httpx.Request("GET", "https://api.github.com/repos/other/repo")),
        (
            "github",
            httpx.Request(
                "POST", f"https://api.github.com/repos/{LIVE_REPOSITORY}/issues"
            ),
        ),
        (
            "embedding",
            httpx.Request(
                "POST",
                "https://provider.example/embeddings",
                json={"input": ["a", "b"]},
            ),
        ),
        (
            "llm",
            httpx.Request(
                "POST",
                "https://provider.example/chat/completions",
                json={"max_tokens": True},
            ),
        ),
    ],
)
async def test_paid_source_guards_reject_scope_expansion_offline(stage, outbound):
    budget = _Budget()
    with pytest.raises(_BudgetExceeded):
        await getattr(budget, stage)(outbound)
    assert not budget.counts


@pytest.mark.parametrize("operation", ["branch", "tree", "blob"])
async def test_source_fixture_allows_only_one_pinned_tree_and_blob(operation):
    budget = _Budget(source_tree_sha="a" * 40, source_blob_sha="b" * 40)
    suffix = {
        "branch": "/branches/main",
        "tree": f"/git/trees/{'a' * 40}?recursive=1",
        "blob": f"/git/blobs/{'b' * 40}",
    }[operation]
    request = httpx.Request(
        "GET", f"https://api.github.com/repos/{LIVE_REPOSITORY}{suffix}"
    )
    await budget.github(request)
    with pytest.raises(_BudgetExceeded):
        await budget.github(request)
    assert budget.counts["github"] == budget.source_requests[operation] == 1


@pytest.mark.parametrize(
    "suffix",
    [
        f"/git/trees/{'c' * 40}?recursive=1",
        f"/git/trees/{'a' * 40}",
        f"/git/blobs/{'c' * 40}",
    ],
)
async def test_source_fixture_rejects_unselected_or_fallback_objects(suffix):
    budget = _Budget(source_tree_sha="a" * 40, source_blob_sha="b" * 40)
    request = httpx.Request(
        "GET", f"https://api.github.com/repos/{LIVE_REPOSITORY}{suffix}"
    )
    with pytest.raises(_BudgetExceeded):
        await budget.github(request)
    assert not budget.counts


async def test_github_fixture_total_request_cap_is_enforced_offline():
    budget = _Budget()
    request = httpx.Request("GET", f"https://api.github.com/repos/{LIVE_REPOSITORY}")
    for _ in range(8):
        await budget.github(request)
    with pytest.raises(_BudgetExceeded):
        await budget.github(request)
    assert budget.counts["github"] == 8


@pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="PostgreSQL integration tests are disabled",
)
async def test_cleanup_removes_owned_source_fixture_but_preserves_other_index(
    session_factory,
):
    repository = f"source-cleanup/repo-{uuid4().hex}"
    owned_id, other_id = uuid4(), uuid4()
    marker = f"smoke-{uuid4().hex[:24]}"
    try:
        async with session_factory() as session, session.begin():
            for index_id, version in (
                (owned_id, marker),
                (other_id, "preserve-control"),
            ):
                session.add(
                    RepositoryIndex(
                        id=index_id,
                        repository=repository,
                        commit_sha="a" * 40,
                        tree_sha="b" * 40,
                        embedding_provider="fixture-cleanup",
                        embedding_model="fixture-test",
                        embedding_dimensions=3,
                        chunker_version=version,
                        status="ready",
                    )
                )
        # Even without a returned index ID or investigation row, the unique marker
        # identifies only the fixture owned by this test.
        await _cleanup(
            session_factory, str(uuid4()), repository, source_chunker_version=marker
        )
        async with session_factory() as session:
            assert await session.get(RepositoryIndex, owned_id) is None
            assert await session.get(RepositoryIndex, other_id) is not None
    finally:
        async with session_factory() as session, session.begin():
            await session.execute(
                delete(RepositoryIndex).where(
                    RepositoryIndex.id.in_((owned_id, other_id))
                )
            )


class _AutomaticProviders(_Providers):
    """Cold-start GitHub fixtures; real builder, leases, embeddings and Graph."""

    def __init__(self, repository, *, over_budget=False):
        super().__init__(repository, source=True)
        self.over_budget = over_budget
        self.content = b"def cleanup():\n    return True\n"
        self.blob_sha = sha1(
            f"blob {len(self.content)}\0".encode() + self.content, usedforsecurity=False
        ).hexdigest()
        self.source_calls = Counter()
        self.document_inputs = []

    async def __call__(self, request):
        path, prefix = request.url.path, f"/repos/{self.repository}"
        if request.url.host == "api.github.com":
            self.requests["github"] += 1
            if path == prefix:
                return httpx.Response(
                    200, json={"private": False, "default_branch": "main"}
                )
            if path == prefix + "/branches/main":
                self.source_calls["branch"] += 1
                return httpx.Response(
                    200,
                    json={
                        "commit": {
                            "sha": "a" * 40,
                            "commit": {"tree": {"sha": "b" * 40}},
                        }
                    },
                )
            if path == prefix + "/git/trees/" + "b" * 40:
                self.source_calls["tree"] += 1
                return httpx.Response(
                    200,
                    json={
                        "sha": "b" * 40,
                        "truncated": False,
                        "tree": [
                            {
                                "path": f"source{n}.py",
                                "type": "blob",
                                "mode": "100644",
                                "sha": self.blob_sha,
                                "size": len(self.content),
                            }
                            for n in range(21 if self.over_budget else 1)
                        ],
                    },
                )
            if path == prefix + "/git/blobs/" + self.blob_sha:
                self.source_calls["blob"] += 1
                return httpx.Response(
                    200,
                    json={
                        "sha": self.blob_sha,
                        "size": len(self.content),
                        "encoding": "base64",
                        "content": base64.b64encode(self.content).decode(),
                    },
                )
            if path == prefix + "/issues":
                return httpx.Response(200, json=[self.issue(124)])
            if path == prefix + "/issues/124/comments":
                return httpx.Response(200, json=[])
            if path == prefix + "/issues/123":
                return httpx.Response(200, json=self.issue(123))
            raise AssertionError(f"Unexpected automatic fixture request: {path}")
        if path.endswith("/embeddings"):
            payload = json.loads(request.content)
            self.requests["embedding"] += 1
            self.document_inputs.extend(payload["input"])
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": n, "embedding": [1.0, 0.0, 0.0]}
                        for n, _ in enumerate(payload["input"])
                    ]
                },
            )
        return await super().__call__(request)

    def issue(self, number):
        return {
            "number": number,
            "title": "Connection cleanup",
            "body": "Cleanup discussion.",
            "state": "open",
            "html_url": f"https://github.com/{self.repository}/issues/{number}",
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-28T00:00:00Z",
        }


@requires_infrastructure
@pytest.mark.parametrize("over_budget", [False, True])
async def test_url_only_cold_start_and_ready_index_reuse_with_real_worker(
    monkeypatch, over_budget
):
    settings = _fake_settings(get_settings())
    repository = f"auto-e2e/repo-{uuid4().hex}"
    providers = _AutomaticProviders(repository, over_budget=over_budget)
    engine = create_db_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    task_ids = []
    try:
        async with _broker(settings) as broker:
            prepared_id = None
            for _ in range(1 if over_budget else 2):
                task_id = str(uuid4())
                task_ids.append(task_id)
                watch = _Watch(
                    factory,
                    task_id,
                    expected_status="failed" if over_budget else "succeeded",
                    snapshot_on_failure=over_budget,
                )
                with monkeypatch.context() as patch:
                    data = await _post_dispatch_read(
                        patch,
                        settings,
                        factory,
                        broker,
                        watch,
                        repository,
                        123,
                        None,
                        automatic=True,
                        transport=httpx.MockTransport(providers),
                    )
                if not over_budget:
                    current = data["prepared_source_index_id"]
                    assert current and (prepared_id is None or prepared_id == current)
                    prepared_id = current
                    assert data["result"]["search_round"] == 2
        if over_budget:
            assert providers.requests["embedding"] == providers.requests["llm"] == 0
            assert providers.source_calls["blob"] == 0
            async with factory() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(RepositoryIndex)
                        .where(RepositoryIndex.repository == repository)
                    )
                    == 0
                )
        else:
            assert providers.source_calls == Counter(branch=2, tree=1, blob=1)
            # Only one source and one unchanged History document are embedded;
            # all other calls are retrieval queries from two Graph executions.
            assert providers.document_inputs.count(providers.content.decode()) == 1
            assert (
                sum(
                    text.startswith("issue #124:") for text in providers.document_inputs
                )
                == 1
            )
            async with factory() as session:
                indexes = (
                    await session.scalars(
                        select(RepositoryIndex).where(
                            RepositoryIndex.repository == repository
                        )
                    )
                ).all()
                assert len(indexes) == 1 and indexes[0].status == "ready"
                assert indexes[0].chunker_version == AUTO_SOURCE_CHUNKER_VERSION
    finally:
        try:
            for task_id in task_ids:
                await _cleanup(
                    factory,
                    task_id,
                    repository,
                    fake=True,
                    source_chunker_version=AUTO_SOURCE_CHUNKER_VERSION,
                )
        finally:
            await engine.dispose()


@dataclass
class _AutomaticBudget(_Budget):
    """Hard paid-smoke caps for the actual automatic small-repository path."""

    limits: dict = field(
        default_factory=lambda: {"github": 30, "embedding": 12, "llm": 7}
    )
    branch: str | None = None
    tree: str | None = None
    blobs: set = field(default_factory=set)
    history_number: int | None = None
    embedding_characters: int = 0

    async def github(self, request):
        prefix = f"/repos/{LIVE_REPOSITORY}"
        path, params = request.url.path, dict(request.url.params)
        operation = None
        allowed = path in (prefix, f"{prefix}/issues/{TARGET_ISSUE}") and not params
        if (
            self.branch is not None
            and path == f"{prefix}/branches/{self.branch}"
            and not params
        ):
            operation, allowed = "branch", True
        if (
            self.tree is not None
            and path == f"{prefix}/git/trees/{self.tree}"
            and params == {"recursive": "1"}
        ):
            operation, allowed = "tree", True
        if path.removeprefix(f"{prefix}/git/blobs/") in self.blobs and not params:
            operation, allowed = "blob", True
        if path == f"{prefix}/issues" and params == {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "page": "1",
            "per_page": "1",
        }:
            operation, allowed = "history", True
        if self.history_number is not None:
            if path == f"{prefix}/issues/{self.history_number}/comments" and params == {
                "page": "1",
                "per_page": "5",
            }:
                operation, allowed = "comments", True
            if path == f"{prefix}/pulls/{self.history_number}" and not params:
                operation, allowed = "pull", True
        if (
            request.method != "GET"
            or request.url.scheme != "https"
            or request.url.host != "api.github.com"
            or not allowed
        ):
            raise _BudgetExceeded("Automatic smoke GitHub scope exceeded")
        if operation and self.source_requests[operation] >= (
            20 if operation == "blob" else 1
        ):
            raise _BudgetExceeded("Automatic smoke operation cap reached")
        self.charge("github")
        if operation:
            self.source_requests[operation] += 1

    async def embedding(self, request):
        payload = self.payload(request, "/embeddings")
        inputs = payload.get("input")
        if (
            not isinstance(inputs, list)
            or not 1 <= len(inputs) <= 20
            or any(
                not isinstance(item, str) or not 1 <= len(item) <= 6000
                for item in inputs
            )
        ):
            raise _BudgetExceeded("Automatic smoke embedding input limit reached")
        characters = sum(map(len, inputs))
        if self.embedding_characters + characters > 120_000:
            raise _BudgetExceeded("Automatic smoke embedding character cap reached")
        self.charge("embedding")
        self.embedding_characters += characters

    def response(self, stage):
        original = super().response(stage)

        async def record(response):
            await original(response)
            if stage != "github" or not response.is_success:
                return
            await response.aread()
            path, prefix = response.request.url.path, f"/repos/{LIVE_REPOSITORY}"
            payload = response.json()
            if path == prefix:
                self.branch = payload.get("default_branch")
            elif path.startswith(prefix + "/branches/"):
                self.tree = payload["commit"]["commit"]["tree"]["sha"]
            elif path.startswith(prefix + "/git/trees/"):
                if payload["truncated"]:
                    raise _BudgetExceeded(
                        "Automatic paid smoke does not traverse fallback trees"
                    )
                self.blobs = {
                    entry["sha"]
                    for entry in payload["tree"]
                    if entry["type"] == "blob"
                    and 0 <= entry.get("size", -1) <= 512 * 1024
                }
            elif path == prefix + "/issues" and payload:
                self.history_number = payload[0]["number"]

        return record


@pytest.mark.skipif(
    not infrastructure_enabled or os.getenv("RUN_LIVE_AUTO_PREPARATION_SMOKE") != "1",
    reason="Automatic paid smoke requires explicit live/DB/broker flags",
)
async def test_paid_url_only_automatic_preparation_worker_smoke(monkeypatch):
    """Cold DB knowledge, real public GitHub/embedding/LLM, no supplied UUID.

    Use a disposable test database and stop other dispatchers/workers. Request
    hooks cap the actual small profile independently of production budgets.
    Knowledge predating the test is preserved by skipping before submission.
    """
    settings = get_settings()
    for name in (
        "llm_base_url",
        "llm_model_name",
        "embedding_base_url",
        "embedding_model",
    ):
        if not getattr(settings, name):
            pytest.fail(f"Configure {name.upper()} before enabling the paid smoke")
    if (
        settings.llm_api_key is None
        or not settings.llm_api_key.get_secret_value().strip()
    ):
        pytest.fail("Configure LLM_API_KEY before enabling the paid smoke")
    task_id, budget = str(uuid4()), _AutomaticBudget()
    async with AsyncExitStack() as stack:
        engine = create_db_engine(settings.database_url.get_secret_value())
        stack.push_async_callback(engine.dispose)
        factory = create_session_factory(engine)
        async with factory() as session, session.begin():
            started_at = await session.scalar(select(func.clock_timestamp()))
            history_exists = await session.scalar(
                select(RepositoryHistoryRecord.number)
                .where(RepositoryHistoryRecord.repository == LIVE_REPOSITORY)
                .limit(1)
            )
            index_exists = await session.scalar(
                select(RepositoryIndex.id)
                .where(
                    RepositoryIndex.repository == LIVE_REPOSITORY,
                    RepositoryIndex.chunker_version == AUTO_SOURCE_CHUNKER_VERSION,
                )
                .limit(1)
            )
        if history_exists is not None or index_exists is not None:
            pytest.skip(
                "Use a cold isolated DB without sampleproject History or automatic indexes; existing knowledge is preserved"
            )

        async def cleanup():
            async with factory() as session, session.begin():
                await session.execute(
                    delete(InvestigationTask).where(InvestigationTask.id == task_id)
                )
                other_task = await session.scalar(
                    select(InvestigationTask.id)
                    .where(InvestigationTask.repository == LIVE_REPOSITORY)
                    .limit(1)
                )
                if other_task is None:
                    await session.execute(
                        delete(RepositoryHistoryRecord).where(
                            RepositoryHistoryRecord.repository == LIVE_REPOSITORY,
                            RepositoryHistoryRecord.fetched_at >= started_at,
                        )
                    )
                    await session.execute(
                        delete(RepositoryIndex).where(
                            RepositoryIndex.repository == LIVE_REPOSITORY,
                            RepositoryIndex.chunker_version
                            == AUTO_SOURCE_CHUNKER_VERSION,
                            RepositoryIndex.created_at >= started_at,
                        )
                    )

        stack.push_async_callback(cleanup)
        async with asyncio.timeout(450):
            async with _broker(settings) as broker:
                watch = _Watch(factory, task_id, budget=budget)
                data = await _post_dispatch_read(
                    monkeypatch,
                    settings,
                    factory,
                    broker,
                    watch,
                    LIVE_REPOSITORY,
                    TARGET_ISSUE,
                    None,
                    automatic=True,
                    budget=budget,
                )
            assert (
                data["prepared_source_index_id"] and data["result"]["source_evidence"]
            )
            assert 1 <= data["result"]["search_round"] <= 3
            assert data["knowledge_budget"]["profile"] == "small"
            assert (
                budget.source_requests["tree"] == 1
                and budget.source_requests["blob"] >= 1
            )
            assert all(status == 200 for status in budget.statuses.values())
            print(
                {
                    "repository": LIVE_REPOSITORY,
                    "issue_number": TARGET_ISSUE,
                    "prepared_source_index_id": data["prepared_source_index_id"],
                    "next_step": data["result"]["next_step"],
                    "search_round": data["result"]["search_round"],
                    **budget.diagnostics(),
                }
            )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.github.com/repos/other/repo/issues/225",
        "http://api.github.com/repos/pypa/sampleproject/issues/225",
        "https://api.github.com/repos/pypa/sampleproject/git/blobs/" + "a" * 40,
        "https://api.github.com/repos/pypa/sampleproject/issues?per_page=100&page=1",
    ],
)
async def test_automatic_paid_scope_guards_are_offline(url):
    with pytest.raises(_BudgetExceeded):
        await _AutomaticBudget().github(httpx.Request("GET", url))


async def test_automatic_paid_embedding_caps_reject_large_batches_offline():
    budget = _AutomaticBudget()
    with pytest.raises(_BudgetExceeded):
        await budget.embedding(
            httpx.Request(
                "POST",
                "https://embedding.example/embeddings",
                json={"input": ["x"] * 21},
            )
        )
    budget.embedding_characters = 120_000
    with pytest.raises(_BudgetExceeded):
        await budget.embedding(
            httpx.Request(
                "POST", "https://embedding.example/embeddings", json={"input": ["x"]}
            )
        )
    assert budget.counts["embedding"] == 0


@pytest.mark.parametrize(
    "usage,expected",
    [
        (
            {
                "prompt_tokens": 1200,
                "completion_tokens": 1500,
                "total_tokens": 2700,
                "completion_tokens_details": {"reasoning_tokens": 1400},
            },
            {
                "prompt_tokens": 1200,
                "completion_tokens": 1500,
                "total_tokens": 2700,
                "reasoning_tokens": 1400,
            },
        ),
        (
            {
                "prompt_tokens": True,
                "completion_tokens": -1,
                "total_tokens": "2700",
                "completion_tokens_details": {"reasoning_tokens": False},
            },
            {},
        ),
        (None, {}),
    ],
)
async def test_automatic_smoke_completion_diagnostics_are_numeric_and_content_safe(
    usage, expected
):
    budget = _AutomaticBudget()
    response = httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": "private-analysis",
                        "reasoning_content": "private-reasoning",
                    },
                }
            ],
            "usage": usage,
        },
    )
    await budget.response("llm")(response)
    diagnostics = budget.diagnostics()
    assert diagnostics["finish_reasons"] == ["length"]
    assert diagnostics["completion_metrics"] == [
        {
            "content_characters": len("private-analysis"),
            "reasoning_content_characters": len("private-reasoning"),
            **expected,
        }
    ]
    assert "private-analysis" not in json.dumps(diagnostics)
    assert "private-reasoning" not in json.dumps(diagnostics)


async def test_automatic_smoke_diagnostics_allow_missing_choice_and_usage():
    budget = _AutomaticBudget()
    await budget.response("llm")(httpx.Response(200, json={"choices": []}))
    assert budget.diagnostics()["finish_reasons"] == ["other"]
    assert budget.diagnostics()["completion_metrics"] == [
        {"content_characters": 0, "reasoning_content_characters": 0}
    ]


@pytest.mark.parametrize("budget_type", [_Budget, _AutomaticBudget])
@pytest.mark.parametrize("tokens", [True, False, 0, -1, 4096.0, 4097, "4096", None])
async def test_source_smoke_rejects_invalid_or_excessive_output_budgets_offline(
    budget_type, tokens
):
    budget = budget_type()
    request = httpx.Request(
        "POST", "https://provider.example/chat/completions", json={"max_tokens": tokens}
    )
    with pytest.raises(_BudgetExceeded):
        await budget.llm(request)
    assert not budget.counts


@requires_infrastructure
async def test_checkpointed_worker_redelivery_reuses_cold_preparation_and_retrieval(
    monkeypatch,
):
    """Cancel a real Worker in analysis, then recover the unacked delivery.

    SQL and RabbitMQ are real; GitHub/embedding/LLM use MockTransport. Lease
    expiry is advanced explicitly only for this owned test Task. No paid calls.
    """
    settings = _fake_settings(get_settings())
    repository, task_id = f"checkpoint-e2e/repo-{uuid4().hex}", str(uuid4())
    providers = _AutomaticProviders(repository)
    entered, cleaned = asyncio.Event(), asyncio.Event()
    first_analysis = True

    async def transport(request):
        nonlocal first_analysis
        if (
            first_analysis
            and request.url.path.endswith("/chat/completions")
            and json.loads(request.content).get("tool_choice") is None
        ):
            first_analysis = False
            providers.requests["llm"] += 1
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return await providers(request)

    engine = create_db_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    app = FastAPI()
    app.state.session_factory = factory
    app.include_router(router)
    monkeypatch.setattr(submission, "uuid4", lambda: UUID(task_id))
    original_claim = dispatcher.claim_next_outbox_event

    async def scoped_claim(session):
        claimed = await original_claim(session)
        if claimed is not None and claimed.task_id != task_id:
            raise AssertionError("Recovery E2E requires an isolated Outbox")
        return claimed

    monkeypatch.setattr(dispatcher, "claim_next_outbox_event", scoped_claim)
    redeliveries = []

    class RecoveryWatch(_Watch):
        async def before_ack(self, message):
            redeliveries.append(message.redelivered)
            await super().before_ack(message)

    watch = RecoveryWatch(factory, task_id)
    try:
        async with (
            _broker(settings) as broker,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client,
        ):
            response = await client.post(
                "/investigations/from-url",
                json={
                    "issue_url": f"https://github.com/{repository}/issues/123",
                    "knowledge_budget_profile": "small",
                },
            )
            assert response.status_code == 202 and response.json()["task_id"] == task_id
            assert await dispatcher.dispatch_one_outbox_event(factory, broker.exchange)
            with monkeypatch.context() as patch:
                ready, clients = _observe_worker(
                    patch,
                    settings,
                    broker,
                    watch,
                    transport=httpx.MockTransport(transport),
                )
                operation = asyncio.create_task(worker.run_worker())
                try:
                    await _wait(operation, ready)
                    await _wait(operation, entered)
                    async with factory() as session:
                        task, snapshot, result = await get_investigation_details(
                            session, task_id
                        )
                        assert (
                            task.status == "running"
                            and snapshot is not None
                            and result is None
                        )
                        old_token, index_id = (
                            task.claim_token,
                            task.prepared_source_index_id,
                        )
                        run = await session.get(InvestigationGraphRun, task_id)
                        frames = await session.scalar(
                            select(func.count())
                            .select_from(InvestigationCheckpoint)
                            .where(InvestigationCheckpoint.task_id == task_id)
                        )
                    assert old_token and index_id and run is not None and frames >= 3
                    before = providers.requests.copy()
                    document_inputs = list(providers.document_inputs)
                finally:
                    await _stop(operation)
                assert cleaned.is_set() and watch.acknowledgements == 0
                assert len(clients) == 3 and all(client.is_closed for client in clients)
            # A crashed owner keeps a running Task until its existing lease expires.
            async with factory() as session, session.begin():
                await session.execute(
                    update(InvestigationTask)
                    .where(InvestigationTask.id == task_id)
                    .values(
                        lease_expires_at=func.clock_timestamp() - timedelta(seconds=1)
                    )
                )
            with monkeypatch.context() as patch:
                ready, clients = _observe_worker(
                    patch,
                    settings,
                    broker,
                    watch,
                    transport=httpx.MockTransport(transport),
                )
                operation = asyncio.create_task(worker.run_worker())
                try:
                    await _wait(operation, ready)
                    await _wait(operation, watch.event)
                    detail = await client.get(f"/investigations/{task_id}")
                    assert detail.status_code == 200
                    data = detail.json()
                    assert data["status"] == "succeeded" and data[
                        "prepared_source_index_id"
                    ] == str(index_id)
                    assert data["result"]["search_round"] == 2
                    assert data["result"]["next_step"] == "needs_more_evidence"
                    assert data["report_markdown"]
                finally:
                    await _stop(operation)
                assert len(clients) == 3 and all(client.is_closed for client in clients)
            assert redeliveries == [True] and watch.acknowledgements == 1
            assert providers.requests["github"] == before["github"]
            # Only the follow-up Source tool embeds a query after recovery.
            assert providers.requests["embedding"] == before["embedding"] + 1
            assert providers.document_inputs[: len(document_inputs)] == document_inputs
            assert providers.source_calls == Counter(branch=1, tree=1, blob=1)
            async with factory() as session:
                restored_run = await session.get(InvestigationGraphRun, task_id)
                assert restored_run.fingerprint == run.fingerprint
                assert restored_run.request_payload == run.request_payload
    finally:
        try:
            await _cleanup(
                factory,
                task_id,
                repository,
                fake=True,
                source_chunker_version=AUTO_SOURCE_CHUNKER_VERSION,
            )
        finally:
            await engine.dispose()
