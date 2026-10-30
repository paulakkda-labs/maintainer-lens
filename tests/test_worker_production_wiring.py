"""Default Worker assembly with real Graph/provider adapters and fake I/O.

No .env, PostgreSQL, RabbitMQ or paid APIs. HTTPX MockTransport supplies public
GitHub, embedding and chat responses. DB helpers/commit scopes and broker
resources are simulated; this does not verify actual SQL fencing or live E2E.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call
from uuid import UUID

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.exc import SQLAlchemyError

import maintainer_lens.agents.investigation.execution as tool_controls
import maintainer_lens.clients.github_http as github_http
import maintainer_lens.investigation_runner as runner
import maintainer_lens.messaging.handler as handler
import maintainer_lens.services.investigation_checkpoint_execution as execution
import maintainer_lens.services.investigation_processor as processor
import maintainer_lens.services.issue_investigator as issue_service
import maintainer_lens.worker as worker
from maintainer_lens.core.config import Settings
from maintainer_lens.db.models.repository_history_embedding import (
    RepositoryHistoryEmbedding,
)
from maintainer_lens.db.models.repository_index import RepositoryChunk, RepositoryIndex
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.messaging.topology import DEAD_ROUTING_KEY, RETRY_ROUTING_KEY
from maintainer_lens.schemas.investigation_checkpoint import (
    FrozenInvestigationRun,
    execution_fingerprint,
)

pytestmark = pytest.mark.anyio
REPOSITORY = "encode/httpx"
ISSUE_NUMBER = 3712
CLAIM_TOKEN = "claim-token-001"
INDEX_ID = UUID("12345678-1234-4234-8234-123456789abc")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(**changes) -> Settings:
    values = {
        "database_url": "postgresql+asyncpg://worker:unused@127.0.0.1/unused",
        "rabbitmq_url": "amqp://worker:unused@127.0.0.1/unused",
        "github_token": "github-test-token",
        "llm_base_url": "https://chat.example/v1",
        "llm_model_name": "test-analysis-model",
        "llm_api_key": "llm-test-token",
        "llm_max_output_tokens": 4096,
        "llm_review_max_output_tokens": None,
        "embedding_base_url": "https://embedding.example/v1",
        "embedding_model": "test-embedding-model",
        "embedding_api_key": "embedding-test-token",
        "embedding_dimensions": 2,
        "embedding_max_batch_size": 5,
        "embedding_request_dimensions": None,
    }
    values.update(changes)
    return Settings(_env_file=None, **values)


class _TrackedClient(httpx.AsyncClient):
    def __init__(self, harness, name, **kwargs):
        super().__init__(transport=httpx.MockTransport(harness.http_response), **kwargs)
        self.harness = harness
        self.name = name
        harness.clients.append(self)

    async def __aenter__(self):
        await super().__aenter__()
        self.harness.acquired.append(self.name)
        return self

    async def __aexit__(self, *args):
        try:
            return await super().__aexit__(*args)
        finally:
            self.harness.events.append(f"close:{self.name}")


class _Harness:
    def __init__(self):
        self.settings = _settings()
        self.events = []
        self.acquired = []
        self.clients = []
        self.requests = []
        self.statements = []
        self.saved_results = []
        self.open_sessions = 0
        self.transactions = 0
        self.status = "pending"
        self.snapshot_committed = False
        self.result_committed = False
        self.require_result = True
        self.empty_history = False
        self.needs_more = False
        self.novel_history = False
        self.history_calls = 0
        self.tool_selections = 0
        self.tool_name = "search_history"
        self.duplicate_tool = False
        self.stored_source_id = None
        self.source_index = RepositoryIndex(
            id=INDEX_ID,
            repository=REPOSITORY,
            commit_sha="a" * 40,
            tree_sha="b" * 40,
            embedding_provider="openai-compatible",
            embedding_model="test-embedding-model",
            embedding_dimensions=2,
            chunker_version="worker-test",
            status="ready",
        )
        self.invalid_citation = False
        self.fail_at = None
        self.fail_commit = None
        self.result_owned = True
        self.block_http = None
        self.block_started = asyncio.Event()
        self.block_cleaned = asyncio.Event()
        self.http_error = None
        self.http_error_stage = "llm"
        self.http_status = {}
        self.publish_fails = False
        self.failure = RuntimeError("Injected startup failure")
        self.commit_error = SQLAlchemyError("Injected result commit failure")
        self.session_factory = self.session
        self.engine = SimpleNamespace(
            dispose=AsyncMock(side_effect=self.dispose_engine)
        )
        self.request = InvestigationRequestedMessage(
            task_id="task-001", repository=REPOSITORY, issue_number=ISSUE_NUMBER
        )
        self.message = SimpleNamespace(
            body=self.request.model_dump_json().encode(),
            headers={},
            message_id=self.request.task_id,
            content_type="application/json",
            ack=AsyncMock(side_effect=self.ack),
        )
        self.queue = SimpleNamespace(iterator=self.queue_iterator)
        self.consumer_channel = SimpleNamespace(
            close=AsyncMock(side_effect=lambda: self.events.append("close:consumer")),
            get_queue=AsyncMock(side_effect=self.get_queue),
            set_qos=AsyncMock(side_effect=self.set_qos),
        )
        self.publisher_channel = SimpleNamespace(
            close=AsyncMock(side_effect=lambda: self.events.append("close:publisher")),
        )
        self.connection = SimpleNamespace(
            close=AsyncMock(side_effect=lambda: self.events.append("close:connection")),
            channel=AsyncMock(side_effect=self.channel),
        )
        self.exchange = SimpleNamespace(publish=AsyncMock(side_effect=self.publish))
        self.channel_count = 0
        self.provider_count = 0

    def check_failure(self, stage):
        if self.fail_at == stage:
            raise self.failure

    def create_engine(self, url):
        assert url == self.settings.database_url.get_secret_value()
        self.acquired.append("engine")
        return self.engine

    async def dispose_engine(self):
        self.events.append("close:engine")

    def create_factory(self, engine):
        assert engine is self.engine
        self.check_failure("session_factory")
        return self.session_factory

    def github_client(self, **kwargs):
        self.check_failure("github_client")
        return _TrackedClient(self, "github", **kwargs)

    def provider_client(self, **kwargs):
        self.provider_count += 1
        name = "embedding" if self.provider_count == 1 else "llm"
        self.check_failure(f"{name}_client")
        return _TrackedClient(self, name, **kwargs)

    async def connect(self, url):
        assert url == self.settings.rabbitmq_url.get_secret_value()
        self.check_failure("connection")
        self.acquired.append("connection")
        return self.connection

    async def channel(self, **kwargs):
        self.channel_count += 1
        name = "consumer" if self.channel_count == 1 else "publisher"
        self.check_failure(name)
        self.acquired.append(name)
        return self.consumer_channel if name == "consumer" else self.publisher_channel

    async def declare(self, channel):
        assert channel is self.publisher_channel
        self.check_failure("topology")
        return self.exchange

    async def get_queue(self, name, *, ensure):
        assert name == worker.QUEUE_NAME and ensure is True
        self.check_failure("queue")
        return self.queue

    async def set_qos(self, *, prefetch_count):
        assert prefetch_count == 1
        self.check_failure("qos")

    @asynccontextmanager
    async def queue_iterator(self):
        self.check_failure("iterator")

        async def messages():
            yield self.message

        yield messages()

    async def ack(self):
        self.events.append("ack")

    async def publish(self, *, message, routing_key, mandatory):
        assert mandatory is True
        assert self.message.ack.await_count == 0
        if self.publish_fails:
            raise self.failure
        self.events.append(f"publish:{routing_key}")

    @asynccontextmanager
    async def session(self):
        self.open_sessions += 1
        session = SimpleNamespace(
            role="read",
            execute=AsyncMock(side_effect=self.query),
            get=AsyncMock(side_effect=self.get_index),
            scalar=AsyncMock(return_value=self.source_index),
        )
        session.begin = lambda: self.transaction(session)
        try:
            yield session
        finally:
            self.open_sessions -= 1

    @asynccontextmanager
    async def transaction(self, session):
        self.transactions += 1
        try:
            yield
            if self.fail_commit == session.role:
                raise self.commit_error
        except BaseException:
            self.events.append(f"rollback:{session.role}")
            raise
        else:
            self.events.append(f"commit:{session.role}")
            if session.role == "snapshot":
                self.snapshot_committed = True
            elif session.role == "result":
                self.result_committed = True
        finally:
            self.transactions -= 1

    async def get_index(self, model, index_id, **kwargs):
        assert model is RepositoryIndex and index_id == INDEX_ID
        assert self.transactions == 1
        self.events.append("source-preflight")
        return self.source_index

    async def query(self, statement):
        if statement.column_descriptions[0]["entity"] is RepositoryChunk:
            assert self.transactions == 1
            chunk = RepositoryChunk(
                index_id=INDEX_ID,
                chunk_key="c" * 64,
                file_path="client.py",
                blob_sha="b" * 40,
                language="python",
                kind="code",
                symbol="cleanup",
                start_line=1,
                end_line=2,
                content="async def cleanup(): pass",
                symbols=[],
                heading_path=[],
                headings=[],
            )
            return SimpleNamespace(all=lambda: [(chunk, 0.1)])
        assert self.snapshot_committed
        assert self.transactions == 0
        self.statements.append(statement)
        self.events.append("history-query")
        self.history_calls += 1
        number = 3782 + self.history_calls - 1 if self.novel_history else 3782
        record = RepositoryHistoryEmbedding(
            repository=REPOSITORY,
            source_key=f"item:{number}",
            source_type="item",
            number=number,
            comment_id=None,
            title="Historical cleanup discussion",
            content="Cancellation can interrupt connection cleanup.",
            source_url=f"https://github.com/{REPOSITORY}/issues/{number}",
            source_updated_at=datetime(2026, 10, 28, tzinfo=UTC),
        )
        rows = [] if self.empty_history else [(record, 0.1)]
        return SimpleNamespace(all=lambda: rows)

    def check_claim(self, session, task_id, claim_token, role):
        assert self.transactions == 1
        assert task_id == self.request.task_id
        assert claim_token == CLAIM_TOKEN
        session.role = role

    async def claim(self, *, session, task_id):
        assert self.transactions == 1 and task_id == self.request.task_id
        session.role = "claim"
        self.status = "running"
        return CLAIM_TOKEN

    async def identity(self, session, *, task_id, claim_token):
        self.check_claim(session, task_id, claim_token, "identity")
        return REPOSITORY, ISSUE_NUMBER, self.stored_source_id

    async def save_snapshot(self, session, *, task_id, claim_token, snapshot):
        self.check_claim(session, task_id, claim_token, "snapshot")
        assert snapshot.title == "Cancellation cleanup"
        return True

    async def save_result(self, session, *, task_id, claim_token, result):
        self.check_claim(session, task_id, claim_token, "result")
        assert self.snapshot_committed
        self.saved_results.append(result)
        self.events.append("result-write")
        return self.result_owned

    async def succeed(self, *, session, task_id, claim_token):
        self.check_claim(session, task_id, claim_token, "success")
        if self.require_result:
            assert self.result_committed
        self.status = "succeeded"
        return True

    async def fail(self, *, session, task_id, claim_token):
        self.check_claim(session, task_id, claim_token, "failed")
        self.status = "failed"
        return True

    async def retry(self, *, session, task_id, claim_token):
        self.check_claim(session, task_id, claim_token, "retry")
        self.status = "pending"
        return True

    async def task_status(self, *, session, task_id):
        assert task_id == self.request.task_id
        return self.status

    async def http_response(self, request):
        self.requests.append(request)
        assert self.transactions == 0
        if request.url.host == "api.github.com":
            stage = "github"
            expected_token = "github-test-token"
            assert self.open_sessions == 0
        elif request.url.host == "embedding.example":
            stage = "embedding"
            expected_token = "embedding-test-token"
        elif request.url.host == "chat.example":
            stage = "llm"
            expected_token = "llm-test-token"
            assert self.open_sessions == 0
        else:
            raise AssertionError(f"Unexpected offline endpoint: {request.url.host}")
        assert request.headers["Authorization"] == f"Bearer {expected_token}"

        if self.block_http == stage:
            self.block_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.block_cleaned.set()
        if self.http_error is not None and stage == self.http_error_stage:
            raise self.http_error
        if stage in self.http_status:
            return httpx.Response(
                self.http_status[stage], json={"error": "Mock failure"}
            )

        if stage == "github":
            if request.url.path == f"/repos/{REPOSITORY}":
                return httpx.Response(200, json={"private": False})
            assert request.url.path == f"/repos/{REPOSITORY}/issues/{ISSUE_NUMBER}"
            return httpx.Response(
                200,
                json={
                    "number": ISSUE_NUMBER,
                    "title": "Cancellation cleanup",
                    "body": None,
                    "state": "open",
                    "html_url": f"https://github.com/{REPOSITORY}/issues/{ISSUE_NUMBER}",
                    "created_at": "2026-10-01T00:00:00Z",
                    "updated_at": "2026-10-28T00:00:00Z",
                },
            )
        payload = json.loads(request.content)
        if stage == "embedding":
            assert request.url.path == "/v1/embeddings"
            assert payload["model"] == "test-embedding-model"
            assert len(payload["input"]) == 1
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
            )
        assert request.url.path == "/v1/chat/completions"
        assert payload["model"] == "test-analysis-model"
        assert payload["thinking"] == {"type": "disabled"}
        assert "Simplified Chinese" in payload["messages"][0]["content"]
        if payload.get("tool_choice") in ("required", "none"):
            phase = "selection" if payload["tool_choice"] == "required" else "feedback"
            if self.block_http == phase:
                self.block_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.block_cleaned.set()
            allowed = [tool["function"]["name"] for tool in payload["tools"]]
            assert allowed == (
                ["search_history", "search_source"]
                if self.request.source_index_id
                else ["search_history"]
            )
            if phase == "selection":
                self.tool_selections += 1
                query = (
                    "Cancellation cleanup"
                    if self.duplicate_tool
                    else f"cleanup followup {self.tool_selections}"
                )
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": f"call-{self.tool_selections}",
                                            "type": "function",
                                            "function": {
                                                "name": self.tool_name,
                                                "arguments": json.dumps(
                                                    {"query": query}
                                                ),
                                            },
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                )
            assert (
                payload["messages"][-1]["tool_call_id"]
                == f"call-{self.tool_selections}"
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": "Tool feedback is not a finding."},
                        }
                    ]
                },
            )
        assert payload["max_tokens"] == 4096
        context = json.loads(payload["messages"][1]["content"])
        assert context["repository"] == REPOSITORY
        assert "history:item:3782" in {
            item["evidence_id"] for item in context["evidence"]
        }
        analysis = {
            "summary": "找到相关历史讨论。",
            "findings": [
                {
                    "statement": "历史记录讨论清理。",
                    "evidence_ids": [
                        "history:item:invented"
                        if self.invalid_citation
                        else "history:item:3782"
                    ],
                }
            ],
            "hypotheses": [],
            "unanswered_questions": ["还需要什么证据？"] if self.needs_more else [],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(analysis)},
                    }
                ]
            },
        )

    def install(self, monkeypatch):
        from maintainer_lens.schemas.investigation_budget import (
            make_knowledge_preparation_budget,
        )

        monkeypatch.setattr(
            worker,
            "load_approved_review",
            AsyncMock(return_value=(make_knowledge_preparation_budget(), None)),
        )
        # This suite isolates SQL. Durable Saver/claim recovery has dedicated tests.
        monkeypatch.setattr(
            worker,
            "InvestigationProgressReporter",
            lambda *args: SimpleNamespace(phase=AsyncMock(), node=AsyncMock()),
        )
        monkeypatch.setattr(worker, "append_task_event", AsyncMock())
        monkeypatch.setattr(
            handler, "get_investigation_task_generation", AsyncMock(return_value=0)
        )

        def checkpoint_store(factory, *, task_id, claim_token):
            assert factory is self.session_factory

            async def create_run(request, config):
                assert self.snapshot_committed and self.transactions == 0
                return FrozenInvestigationRun(
                    task_id=task_id,
                    request=request,
                    execution=config,
                    fingerprint=execution_fingerprint(request, config),
                )

            return SimpleNamespace(
                task_id=task_id,
                claim_token=claim_token,
                load_run=AsyncMock(return_value=None),
                create_run=create_run,
            )

        monkeypatch.setattr(worker, "InvestigationCheckpointStore", checkpoint_store)

        class MemorySaver(InMemorySaver):
            def cancel_pending(self, *, exclude):
                pass  # This suite replaces SQL; no pending database I/O exists.

        monkeypatch.setattr(
            execution, "InvestigationCheckpointSaver", lambda store: MemorySaver()
        )
        monkeypatch.setattr(worker, "get_settings", lambda: self.settings)
        monkeypatch.setattr(
            worker, "create_db_engine", Mock(side_effect=self.create_engine)
        )
        monkeypatch.setattr(
            worker, "create_session_factory", Mock(side_effect=self.create_factory)
        )
        monkeypatch.setattr(
            worker, "create_rabbitmq_connection", AsyncMock(side_effect=self.connect)
        )
        monkeypatch.setattr(
            worker,
            "declare_investigation_topology",
            AsyncMock(side_effect=self.declare),
        )
        # Replace module bindings, not the global httpx class used by other modules.
        monkeypatch.setattr(
            worker,
            "httpx",
            SimpleNamespace(AsyncClient=self.provider_client, Timeout=httpx.Timeout),
        )
        monkeypatch.setattr(
            github_http,
            "httpx",
            SimpleNamespace(
                AsyncClient=self.github_client,
                Timeout=httpx.Timeout,
                Limits=httpx.Limits,
            ),
        )
        monkeypatch.setattr(
            processor, "get_claimed_investigation_identity", self.identity
        )
        monkeypatch.setattr(issue_service, "upsert_issue_snapshot", self.save_snapshot)
        monkeypatch.setattr(processor, "upsert_investigation_result", self.save_result)
        monkeypatch.setattr(runner, "claim_investigation_task", self.claim)
        monkeypatch.setattr(runner, "mark_investigation_succeeded", self.succeed)
        monkeypatch.setattr(runner, "mark_investigation_failed", self.fail)
        monkeypatch.setattr(runner, "release_investigation_task_for_retry", self.retry)
        monkeypatch.setattr(handler, "get_investigation_task_status", self.task_status)

    def assert_closed(self):
        assert self.open_sessions == self.transactions == 0
        assert all(client.is_closed for client in self.clients)
        assert [
            event.removeprefix("close:")
            for event in self.events
            if event.startswith("close:")
        ] == list(reversed(self.acquired))
        self.engine.dispose.assert_awaited_once()


@pytest.fixture
def harness(monkeypatch):
    harness = _Harness()
    harness.install(monkeypatch)
    return harness


@pytest.mark.parametrize(
    "needs_more,empty", [(False, False), (True, False), (True, True)]
)
async def test_default_worker_runs_graph_and_commits_before_success_and_ack(
    harness, monkeypatch, needs_more, empty
):
    harness.needs_more = needs_more
    harness.empty_history = empty
    process = AsyncMock(wraps=processor.process_graph_investigation)
    monkeypatch.setattr(worker, "process_graph_investigation", process)

    await worker.run_worker()

    process.assert_awaited_once()
    options = process.await_args.kwargs
    assert options["task_id"] == "task-001"
    assert options["claim_token"] == CLAIM_TOKEN
    assert options["repository"] == REPOSITORY
    assert options["issue_number"] == ISSUE_NUMBER
    assert options["response_language"] == "zh"
    assert options["max_search_rounds"] == 3
    assert options["timeout_seconds"] == 120.0
    assert options["session_factory"] is harness.session_factory
    assert options["source_searcher"] is None
    assert options["source_index_id"] is None
    assert options["checkpoint_store"].task_id == harness.request.task_id
    assert options["checkpoint_store"].claim_token == CLAIM_TOKEN
    assert options["checkpoint_config"].llm_max_output_tokens == 4096
    assert options["checkpoint_config"].max_search_rounds == 3
    assert options["checkpoint_config"].search_strategy == "tool"
    assert "query_planner" not in options
    assert callable(options["tool_search_runner"])
    assert callable(options["source_index_validator"])
    assert harness.status == "succeeded"
    assert harness.snapshot_committed and harness.result_committed
    result = harness.saved_results[0]
    assert result.search_round == (2 if needs_more and not empty else 1)
    assert result.next_step == (
        "needs_more_evidence" if needs_more or empty else "draft_preliminary_report"
    )
    assert result.source_index_id is None and result.source_evidence == []
    assert len(result.analysis_evidence) == (0 if empty else 1)
    assert len([r for r in harness.requests if r.url.host == "chat.example"]) == (
        0 if empty else (3 if needs_more else 1)
    )
    assert len(harness.statements) == (2 if needs_more and not empty else 1)
    parameters = harness.statements[0].compile().params
    assert parameters["embedding_provider_1"] == "openai-compatible"
    assert parameters["embedding_model_1"] == "test-embedding-model"
    assert parameters["embedding_dimensions_1"] == 2
    assert (
        harness.events.index("commit:result")
        < harness.events.index("commit:success")
        < harness.events.index("ack")
    )
    harness.exchange.publish.assert_not_awaited()
    harness.message.ack.assert_awaited_once()
    harness.connection.channel.assert_has_awaits(
        [
            call(publisher_confirms=False),
            call(publisher_confirms=True, on_return_raises=True),
        ]
    )
    clients = {client.name: client for client in harness.clients}
    assert clients["github"].timeout.read == 15.0
    assert clients["embedding"].timeout.read == 60.0
    assert clients["llm"].timeout.read == 90.0
    harness.assert_closed()


@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_base_url", None),
        ("embedding_model", None),
        ("embedding_base_url", "not-a-url"),
        ("embedding_request_dimensions", 3),
        ("llm_base_url", None),
        ("llm_model_name", None),
        ("llm_api_key", None),
        ("llm_api_key", "   "),
    ],
)
async def test_missing_provider_configuration_fails_before_broker(
    harness, field, value
):
    harness.settings = _settings(**{field: value})
    with pytest.raises(ValueError):
        await worker.run_worker()
    worker.create_rabbitmq_connection.assert_not_awaited()
    harness.message.ack.assert_not_awaited()
    assert harness.requests == []
    assert harness.saved_results == []
    harness.assert_closed()


@pytest.mark.parametrize(
    "stage",
    [
        "session_factory",
        "github_client",
        "embedding_client",
        "llm_client",
        "connection",
        "consumer",
        "publisher",
        "topology",
        "queue",
        "qos",
        "iterator",
    ],
)
async def test_partial_startup_failures_close_every_acquired_resource(harness, stage):
    harness.fail_at = stage
    with pytest.raises(RuntimeError) as caught:
        await worker.run_worker()
    assert caught.value is harness.failure
    harness.message.ack.assert_not_awaited()
    assert harness.requests == []
    harness.assert_closed()


async def test_injected_investigator_bypasses_all_provider_configuration(
    harness, monkeypatch
):
    harness.require_result = False
    harness.settings = _settings(
        llm_base_url=None,
        llm_model_name=None,
        llm_api_key=None,
        embedding_base_url=None,
        embedding_model=None,
        embedding_api_key=None,
    )
    embedding_factory = Mock(side_effect=AssertionError("Must not create embeddings"))
    analyzer_factory = Mock(side_effect=AssertionError("Must not create analyzer"))
    github_factory = Mock(side_effect=AssertionError("Must not create GitHub client"))
    monkeypatch.setattr(worker, "create_embedding_client", embedding_factory)
    monkeypatch.setattr(worker, "create_llm_analyzer", analyzer_factory)
    monkeypatch.setattr(worker, "create_github_http_client", github_factory)
    investigate = AsyncMock()
    await worker.run_worker(investigate=investigate)
    investigate.assert_awaited_once_with(harness.request, CLAIM_TOKEN)
    embedding_factory.assert_not_called()
    analyzer_factory.assert_not_called()
    github_factory.assert_not_called()
    assert harness.clients == []
    harness.message.ack.assert_awaited_once()
    harness.assert_closed()


@pytest.mark.parametrize("publish_fails", [False, True])
@pytest.mark.parametrize(
    "failure",
    [
        "upstream_timeout",
        "embedding_429",
        "llm_401",
        "invalid_citation",
        "result_commit",
    ],
)
async def test_graph_failures_keep_existing_routing_and_confirm_before_ack(
    harness, failure, publish_fails
):
    if failure == "upstream_timeout":
        harness.http_error = httpx.ReadTimeout("Upstream deadline")
    elif failure == "embedding_429":
        harness.http_status["embedding"] = 429
    elif failure == "llm_401":
        harness.http_status["llm"] = 401
    elif failure == "invalid_citation":
        harness.invalid_citation = True
    else:
        harness.fail_commit = "result"
    harness.publish_fails = publish_fails

    if publish_fails:
        with pytest.raises(RuntimeError) as caught:
            await worker.run_worker()
        assert caught.value is harness.failure
        harness.message.ack.assert_not_awaited()
    else:
        await worker.run_worker()
        harness.message.ack.assert_awaited_once()

    permanent = failure in ("invalid_citation", "llm_401")
    destination = DEAD_ROUTING_KEY if permanent else RETRY_ROUTING_KEY
    transition = "failed" if permanent else "retry"
    assert harness.status == ("failed" if permanent else "pending")
    assert not harness.result_committed
    assert "commit:success" not in harness.events
    published = harness.exchange.publish.await_args.kwargs
    assert published["routing_key"] == destination
    assert published["message"].headers["ml-retry-attempts"] == (0 if permanent else 1)
    if not publish_fails:
        assert (
            harness.events.index(f"commit:{transition}")
            < harness.events.index(f"publish:{destination}")
            < harness.events.index("ack")
        )
    harness.assert_closed()


async def test_lost_result_claim_defers_without_releasing_owner(harness):
    harness.result_owned = False
    await worker.run_worker()
    assert harness.status == "running"
    assert not harness.result_committed
    assert "commit:success" not in harness.events
    assert "commit:retry" not in harness.events
    published = harness.exchange.publish.await_args.kwargs
    assert published["routing_key"] == RETRY_ROUTING_KEY
    assert published["message"].headers["ml-retry-attempts"] == 0
    harness.message.ack.assert_awaited_once()
    harness.assert_closed()


async def test_whole_graph_deadline_uses_existing_retry_policy(harness, monkeypatch):
    harness.block_http = "embedding"
    scopes = []

    def deadline(seconds):
        assert seconds == 120.0
        scope = asyncio.timeout(seconds)
        scopes.append(scope)
        return scope

    # Reschedule the real scope after retrieval starts; do not race Graph setup
    # against a tiny wall-clock budget or mock the compiled Graph invocation.
    monkeypatch.setattr(
        execution,
        "asyncio",
        SimpleNamespace(
            timeout=deadline,
            create_task=asyncio.create_task,
            shield=asyncio.shield,
            gather=asyncio.gather,
        ),
    )
    operation = asyncio.create_task(worker.run_worker())
    try:
        async with asyncio.timeout(3):
            await harness.block_started.wait()
            scopes[0].reschedule(asyncio.get_running_loop().time())
            await operation
        assert harness.block_cleaned.is_set()
        assert harness.status == "pending" and not harness.result_committed
        assert (
            harness.exchange.publish.await_args.kwargs["routing_key"]
            == RETRY_ROUTING_KEY
        )
        harness.message.ack.assert_awaited_once()
        harness.assert_closed()
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_external_cancellation_cleans_graph_and_worker_without_ack(harness):
    harness.block_http = "embedding"
    operation = asyncio.create_task(worker.run_worker())
    try:
        async with asyncio.timeout(3):
            await harness.block_started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert harness.block_cleaned.is_set()
        assert harness.status == "running"
        assert not harness.result_committed
        assert "commit:success" not in harness.events
        assert "commit:retry" not in harness.events
        harness.message.ack.assert_not_awaited()
        harness.exchange.publish.assert_not_awaited()
        harness.assert_closed()
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_production_three_round_budget_stops_before_fourth_search(harness):
    harness.needs_more = True
    harness.novel_history = True
    await worker.run_worker()
    result = harness.saved_results[0]
    assert result.search_round == harness.history_calls == 3
    assert harness.tool_selections == 2
    assert result.next_step == "needs_more_evidence"
    assert len(result.history_evidence) == 3
    assert len([r for r in harness.requests if r.url.host == "chat.example"]) == 7
    assert "Tool feedback is not a finding" not in result.model_dump_json()
    harness.assert_closed()


async def test_duplicate_tool_query_skips_retrieval_and_feedback(harness):
    harness.needs_more = True
    harness.duplicate_tool = True
    await worker.run_worker()
    assert harness.history_calls == 1 and harness.tool_selections == 1
    assert harness.saved_results[0].search_round == 1
    assert len([r for r in harness.requests if r.url.host == "chat.example"]) == 2
    harness.assert_closed()


async def test_source_request_preflights_then_binds_source_and_both_tools(harness):
    harness.request = harness.request.model_copy(update={"source_index_id": INDEX_ID})
    harness.message.body = harness.request.model_dump_json().encode()
    harness.stored_source_id = INDEX_ID
    harness.needs_more = True
    harness.tool_name = "search_source"
    await worker.run_worker()
    assert harness.events.index("source-preflight") < harness.events.index(
        "commit:snapshot"
    )
    result = harness.saved_results[0]
    assert result.source_index_id == INDEX_ID
    assert len(result.source_evidence) == 1 and result.search_round == 2
    assert {item["category"] for item in result.analysis_evidence} == {
        "history",
        "source",
    }
    assert harness.tool_selections == 1
    harness.assert_closed()


@pytest.mark.parametrize(
    "case",
    ["missing", "foreign", "building", "failed", "provider", "model", "dimensions"],
)
async def test_invalid_source_preflight_stops_before_external_calls(harness, case):
    harness.request = harness.request.model_copy(update={"source_index_id": INDEX_ID})
    harness.message.body = harness.request.model_dump_json().encode()
    harness.stored_source_id = INDEX_ID
    if case == "missing":
        harness.source_index = None
    elif case == "foreign":
        harness.source_index.repository = "other/repo"
    elif case in ("building", "failed"):
        harness.source_index.status = case
    elif case == "dimensions":
        harness.source_index.embedding_dimensions = 3
    else:
        setattr(harness.source_index, f"embedding_{case}", "incompatible")
    await worker.run_worker()
    retry = case == "building"
    assert harness.status == ("pending" if retry else "failed")
    assert harness.requests == [] and harness.saved_results == []
    assert harness.exchange.publish.await_args.kwargs["routing_key"] == (
        RETRY_ROUTING_KEY if retry else DEAD_ROUTING_KEY
    )
    harness.message.ack.assert_awaited_once()
    harness.assert_closed()


@pytest.mark.parametrize("phase", ["selection", "feedback"])
async def test_tool_deadline_uses_real_scope_and_cleans_requests(
    harness, monkeypatch, phase
):
    harness.needs_more = True
    harness.block_http = phase
    scopes = []

    def deadline(seconds):
        assert seconds == 60.0
        scope = asyncio.timeout(seconds)
        scopes.append(scope)
        return scope

    monkeypatch.setattr(
        tool_controls,
        "asyncio",
        SimpleNamespace(timeout=deadline, CancelledError=asyncio.CancelledError),
    )
    operation = asyncio.create_task(worker.run_worker())
    try:
        async with asyncio.timeout(3):
            await harness.block_started.wait()
            scopes[0].reschedule(asyncio.get_running_loop().time())
            await operation
        assert harness.block_cleaned.is_set()
        assert harness.status == "pending" and not harness.result_committed
        assert (
            harness.exchange.publish.await_args.kwargs["routing_key"]
            == RETRY_ROUTING_KEY
        )
        harness.assert_closed()
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.parametrize("phase", ["selection", "feedback"])
async def test_tool_external_cancellation_keeps_task_and_delivery_unfinished(
    harness, phase
):
    harness.needs_more = True
    harness.block_http = phase
    operation = asyncio.create_task(worker.run_worker())
    try:
        async with asyncio.timeout(3):
            await harness.block_started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert harness.block_cleaned.is_set() and harness.status == "running"
        assert not harness.result_committed
        harness.message.ack.assert_not_awaited()
        harness.exchange.publish.assert_not_awaited()
        harness.assert_closed()
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_model_cannot_call_source_tool_on_history_only_request(harness):
    harness.needs_more = True
    harness.tool_name = "search_source"
    await worker.run_worker()
    assert harness.status == "failed" and harness.history_calls == 1
    assert not harness.result_committed
    assert harness.exchange.publish.await_args.kwargs["routing_key"] == DEAD_ROUTING_KEY
    harness.assert_closed()
