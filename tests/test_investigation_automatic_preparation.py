"""Automatic intake/preparation regressions with no settings, DB or paid APIs."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

import maintainer_lens.api.routes.investigations as routes
import maintainer_lens.services.investigation_preparation as preparation
import maintainer_lens.services.investigation_processor as processor
import maintainer_lens.services.investigation_submission as submission
import maintainer_lens.services.repository_history_pipeline as history
import maintainer_lens.services.repository_index_preparation as source
from maintainer_lens.agents.investigation_analysis import InvestigationAnalysis
from maintainer_lens.api.dependencies import get_session_factory
from maintainer_lens.clients.github_history import GitHubHistoryItem, GitHubHistoryPage
from maintainer_lens.db.investigation_preparation import ClaimedKnowledgePreparation
from maintainer_lens.investigation_runner import LeaseLostError
from maintainer_lens.messaging.publisher import create_investigation_message
from maintainer_lens.messaging.schemas import InvestigationRequestedMessage
from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_url import (
    CreateInvestigationFromUrlRequest,
    parse_issue_url,
)
from maintainer_lens.schemas.repository import (
    RepositoryFile,
    RepositoryManifest,
    RepositoryRevision,
)
from maintainer_lens.services.repository_history_semantic_search import (
    RepositoryHistorySemanticHit,
)

pytestmark = pytest.mark.anyio
REPOSITORY = "org/repo"
INDEX_ID = uuid4()


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Sessions:
    """Track short transactions and prove network awaits happen outside them."""

    def __init__(self):
        self.depth = 0
        self.records = []
        self.session = SimpleNamespace(
            add=self.records.append, flush=AsyncMock(), begin=self.begin
        )

    @asynccontextmanager
    async def __call__(self):
        yield self.session

    @asynccontextmanager
    async def begin(self):
        self.depth += 1
        try:
            yield self.session
        finally:
            self.depth -= 1


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/org/repo/issues/1",
        "https://github.com.evil/org/repo/issues/1",
        "https://user@github.com/org/repo/issues/1",
        "https://github.com:443/org/repo/issues/1",
        "https://github.com/org/repo/pull/1",
        "https://github.com/org/repo/issues/0",
        "https://github.com/org/repo/issues/01",
        "https://github.com/org/repo/issues/true",
        "https://github.com/org/repo/issues/1?redirect=https://evil",
        "https://github.com/../repo/issues/1",
        "https://github.com/org/repo/issues/1#comment",
        "https://github.com/org/%72epo/issues/1",
    ],
)
def test_url_intake_rejects_noncanonical_targets(url):
    with pytest.raises(ValidationError):
        CreateInvestigationFromUrlRequest(issue_url=url)


@pytest.mark.parametrize(
    "injected",
    [
        "source_index_id",
        "repository",
        "auto_prepare_knowledge",
        "knowledge_budget",
        "findings",
    ],
)
def test_url_intake_rejects_internal_fields(injected):
    with pytest.raises(ValidationError):
        CreateInvestigationFromUrlRequest.model_validate(
            {
                "issue_url": "https://github.com/org/repo/issues/123",
                injected: None,
            }
        )


async def test_url_api_queues_auto_mode_with_default_budget(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    factory = object()
    app.dependency_overrides[get_session_factory] = lambda: factory
    create = AsyncMock(
        return_value=InvestigationRequestedMessage(
            task_id="task",
            repository=REPOSITORY,
            issue_number=123,
            auto_prepare_knowledge=True,
        )
    )
    from maintainer_lens.schemas.investigation_reuse import (
        InvestigationFreshness,
        ResolveInvestigationResponse,
    )

    create.return_value = ResolveInvestigationResponse(
        task_id="task",
        status="pending",
        generation=0,
        decision="created",
        freshness=InvestigationFreshness(),
    )
    monkeypatch.setattr(routes, "resolve_investigation_url", create)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/investigations/from-url",
            json={"issue_url": "https://github.com/org/repo/issues/123/"},
        )
        bad = await client.post(
            "/investigations/from-url", json={"issue_url": "https://evil/issues/123"}
        )
    assert response.status_code == 202 and bad.status_code == 422
    create.assert_awaited_once_with(
        factory,
        repository=REPOSITORY,
        issue_number=123,
        knowledge_budget_profile="standard",
    )
    assert parse_issue_url("https://github.com/org/repo/issues/123").issue_number == 123


async def test_auto_submission_snapshots_budget_and_mode_in_one_transaction():
    sessions = _Sessions()
    message = await submission.create_investigation_submission(
        sessions,
        repository=REPOSITORY,
        issue_number=123,
        auto_prepare_knowledge=True,
        knowledge_budget_profile="small",
    )
    task, event = sessions.records
    assert task.auto_prepare_knowledge is True and task.prepared_source_index_id is None
    assert (
        task.source_index_id is None
        and task.knowledge_budget
        == make_knowledge_preparation_budget("small").model_dump(mode="json")
    )
    assert event.payload == message.model_dump(mode="json")
    assert "knowledge_budget" not in event.payload
    parsed = InvestigationRequestedMessage.model_validate_json(
        create_investigation_message(
            "task", REPOSITORY, 123, auto_prepare_knowledge=True
        ).body
    )
    assert parsed.auto_prepare_knowledge and parsed.source_index_id is None
    assert not InvestigationRequestedMessage.model_validate(
        {"task_id": "old", "repository": REPOSITORY, "issue_number": 123}
    ).auto_prepare_knowledge
    with pytest.raises(ValidationError):
        await submission.create_investigation_submission(
            sessions,
            repository=REPOSITORY,
            issue_number=123,
            auto_prepare_knowledge=True,
            source_index_id=INDEX_ID,
        )
    assert len(sessions.records) == 2


def _processor_dependencies(monkeypatch, *, bound=False):
    sessions = _Sessions()
    budget = make_knowledge_preparation_budget("small")
    events, saved = [], []
    monkeypatch.setattr(
        processor,
        "get_claimed_investigation_identity",
        AsyncMock(return_value=(REPOSITORY, 123, None)),
    )
    context = ClaimedKnowledgePreparation(
        REPOSITORY, 123, INDEX_ID if bound else None, budget
    )
    monkeypatch.setattr(
        processor, "get_claimed_knowledge_preparation", AsyncMock(return_value=context)
    )

    async def snapshot(**kwargs):
        assert sessions.depth == 0
        events.append("snapshot")
        return SimpleNamespace(title="Connection cleanup", body=None)

    async def prepare(repository, actual_budget):
        assert (
            sessions.depth == 0 and repository == REPOSITORY and actual_budget == budget
        )
        events.append("prepare")
        return INDEX_ID

    async def validate(repository, index_id):
        assert sessions.depth == 0 and (repository, index_id) == (REPOSITORY, INDEX_ID)
        events.append("validate")

    async def bind(*args, **kwargs):
        assert sessions.depth == 1 and kwargs["source_index_id"] == INDEX_ID
        events.append("bind")
        return True

    async def search(repository, query):
        assert sessions.depth == 0
        events.append("history")
        return (
            RepositoryHistorySemanticHit(
                repository=repository,
                number=124,
                source_type="item",
                comment_id=None,
                title="Cleanup discussion",
                content="Release connection in finally.",
                source_url=f"https://github.com/{repository}/issues/124",
                source_updated_at=datetime(2026, 10, 28, tzinfo=UTC),
                distance=0.1,
            ),
        )

    async def source_search(repository, index_id, query):
        assert sessions.depth == 0 and index_id == INDEX_ID
        events.append("source")
        return []

    async def analyze(request):
        assert sessions.depth == 0
        events.append("analysis")
        return InvestigationAnalysis(
            summary="Relevant history.",
            findings=[
                {
                    "statement": "Cleanup was discussed.",
                    "evidence_ids": ["history:item:124"],
                }
            ],
            hypotheses=[],
            unanswered_questions=[],
        )

    async def save(*args, **kwargs):
        assert sessions.depth == 1
        saved.append(kwargs["result"])
        events.append("save")
        return True

    monkeypatch.setattr(processor, "investigate_issue", snapshot)
    monkeypatch.setattr(processor, "bind_prepared_source_index", bind)
    monkeypatch.setattr(processor, "upsert_investigation_result", save)
    return (
        dict(
            http_client=object(),
            session_factory=sessions,
            task_id="task",
            claim_token="claim",
            repository=REPOSITORY,
            issue_number=123,
            response_language="zh",
            history_searcher=search,
            analyzer=analyze,
            timeout_seconds=5,
            source_searcher=source_search,
            source_index_validator=validate,
            auto_prepare_knowledge=True,
            knowledge_preparer=prepare,
        ),
        events,
        saved,
    )


@pytest.mark.parametrize("bound", [False, True])
async def test_processor_runs_real_graph_and_reuses_bound_preparation(
    monkeypatch, bound
):
    options, events, saved = _processor_dependencies(monkeypatch, bound=bound)
    await processor.process_graph_investigation(**options)
    assert events == (
        ["validate", "snapshot"]
        if bound
        else ["snapshot", "prepare", "validate", "bind"]
    ) + ["history", "source", "analysis", "save"]
    assert len(saved) == 1 and saved[0].source_index_id == INDEX_ID
    assert saved[0].next_step == "draft_preliminary_report"
    processor.get_claimed_investigation_identity.assert_awaited_once_with(
        options["session_factory"].session,
        task_id="task",
        claim_token="claim",
        auto_prepare_knowledge=True,
    )


async def test_stale_claim_cannot_enter_graph_after_preparation(monkeypatch):
    options, events, saved = _processor_dependencies(monkeypatch)
    monkeypatch.setattr(
        processor, "bind_prepared_source_index", AsyncMock(return_value=False)
    )
    with pytest.raises(LeaseLostError):
        await processor.process_graph_investigation(**options)
    assert "analysis" not in events and not saved


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("bad budget"),
        LeaseLostError("lost"),
        ExceptionGroup("group", [RuntimeError("one"), RuntimeError("two")]),
    ],
)
async def test_preflight_failures_preserve_identity_and_prevent_external_calls(
    monkeypatch, failure
):
    options, events, saved = _processor_dependencies(monkeypatch)
    monkeypatch.setattr(
        processor, "get_claimed_knowledge_preparation", AsyncMock(side_effect=failure)
    )
    with pytest.raises(type(failure)) as caught:
        await processor.process_graph_investigation(**options)
    assert caught.value is failure and not events and not saved


async def test_preparation_cancellation_propagates_and_cleans_before_graph(monkeypatch):
    options, events, saved = _processor_dependencies(monkeypatch)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def block(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    options["knowledge_preparer"] = block
    operation = asyncio.create_task(processor.process_graph_investigation(**options))
    await asyncio.wait_for(entered.wait(), 1)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert cleaned.is_set() and not saved and "bind" not in events


def _preparer(monkeypatch, *, build=None, prepare_history=None, accelerated=False):
    sessions = _Sessions()
    builder = build or AsyncMock(return_value=INDEX_ID)
    history_stage = prepare_history or AsyncMock()
    monkeypatch.setattr(preparation, "build_repository_index", builder)
    monkeypatch.setattr(preparation, "prepare_repository_history_pages", history_stage)
    if accelerated:
        real_timeout = asyncio.timeout
        monkeypatch.setattr(
            preparation,
            "asyncio",
            SimpleNamespace(
                timeout=lambda seconds: real_timeout(0.01 if seconds else None)
            ),
        )
    return (
        preparation.create_knowledge_preparer(
            sessions,
            object(),
            object(),
            embedding_provider_name="provider",
            embedding_model="model",
            embedding_dimensions=3,
        ),
        builder,
        history_stage,
    )


@pytest.mark.parametrize("phase", ["source", "history"])
@pytest.mark.parametrize("swallow", [False, True])
async def test_own_preparation_deadline_includes_each_await_and_cannot_be_swallowed(
    monkeypatch, phase, swallow
):
    cleaned = asyncio.Event()

    async def block(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not swallow:
                raise
            return INDEX_ID
        finally:
            cleaned.set()

    prepare, _, _ = _preparer(
        monkeypatch,
        build=block if phase == "source" else None,
        prepare_history=block if phase == "history" else None,
        accelerated=True,
    )
    with pytest.raises(preparation.KnowledgePreparationDeadlineExceeded):
        await prepare(REPOSITORY, make_knowledge_preparation_budget("small"))
    assert cleaned.is_set()


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("upstream"),
        httpx.ReadTimeout("read"),
        ExceptionGroup("original", [ValueError("one"), RuntimeError("two")]),
    ],
)
async def test_preparation_preserves_dependency_errors(monkeypatch, failure):
    prepare, _, followup = _preparer(monkeypatch, build=AsyncMock(side_effect=failure))
    with pytest.raises(type(failure)) as caught:
        await prepare(REPOSITORY, make_knowledge_preparation_budget("small"))
    assert caught.value is failure
    followup.assert_not_awaited()


async def test_unlimited_preparation_passes_null_limits_and_fixed_complete_index_identity(
    monkeypatch,
):
    prepare, builder, followup = _preparer(monkeypatch)
    budget = make_knowledge_preparation_budget("unlimited")
    assert await prepare(REPOSITORY, budget) == INDEX_ID
    assert builder.await_args.kwargs["preparation_budget"] == budget
    assert builder.await_args.kwargs["chunker_version"] == "investigation-auto-v1"
    assert followup.await_args.kwargs["max_items"] is None


def _source_fixture(files=1, *, size=40):
    revision = RepositoryRevision(
        repository=REPOSITORY,
        default_branch="main",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
    )
    manifest = RepositoryManifest(
        **revision.model_dump(exclude={"default_branch"}),
        files=[
            RepositoryFile(
                path=f"file-{n}.py", blob_sha="c" * 40, size=size, mode="100644"
            )
            for n in range(files)
        ],
    )
    return revision, manifest


async def _source_call(monkeypatch, tmp_path, budget, *, files=1, size=40, fail=None):
    revision, manifest = _source_fixture(files, size=size)
    directories = []
    original = source.TemporaryDirectory

    def directory(**kwargs):
        context = original(dir=tmp_path, **kwargs)
        directories.append(Path(context.name))
        return context

    monkeypatch.setattr(source, "TemporaryDirectory", directory)
    blobs = AsyncMock(return_value="def cleanup():\n    pass\n".ljust(size))
    monkeypatch.setattr(source, "fetch_repository_blob", blobs)

    def chunker(item):
        return [
            CodeChunk(
                file=item.file,
                language="python",
                kind="function",
                start_line=1,
                end_line=2,
                content=item.content[:40],
            )
        ]

    monkeypatch.setattr(source, "chunk_repository_source", chunker)
    embedding = SimpleNamespace(
        embed_documents=AsyncMock(side_effect=fail)
        if fail
        else AsyncMock(side_effect=lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
    )

    async def write(session, revision, replay, **kwargs):
        first = list(replay())
        assert first == list(replay()) and len(first) == files
        assert all(len(item.vector) == 3 for item in first)
        return INDEX_ID

    writer = AsyncMock(side_effect=write)
    monkeypatch.setattr(source, "write_repository_index", writer)
    try:
        result = await source.prepare_source_index_contents(
            object(),
            embedding,
            object(),
            revision,
            manifest,
            budget=budget,
            embedding_provider_name="provider",
            embedding_model="model",
            embedding_dimensions=3,
            chunker_version="version",
            embedding_batch_size=2,
            owner_token=uuid4(),
        )
        return result, blobs, embedding, writer
    except source.KnowledgePreparationBudgetExceeded:
        if (
            budget.source_max_files is not None and files > budget.source_max_files
        ) or (
            budget.source_max_bytes is not None
            and files * size > budget.source_max_bytes
        ):
            blobs.assert_not_awaited()
        embedding.embed_documents.assert_not_awaited()
        raise
    finally:
        assert all(not path.exists() for path in directories)


@pytest.mark.parametrize(
    "field,maximum,files,size",
    [
        ("source_max_files", 1, 2, 40),
        ("source_max_bytes", 39, 1, 40),
        ("source_max_chunks", 1, 2, 40),
    ],
)
async def test_finite_source_budgets_fail_before_embedding(
    monkeypatch, tmp_path, field, maximum, files, size
):
    budget = make_knowledge_preparation_budget("small").model_copy(
        update={field: maximum}
    )
    with pytest.raises(source.KnowledgePreparationBudgetExceeded):
        await _source_call(monkeypatch, tmp_path, budget, files=files, size=size)
    source.write_repository_index.assert_not_awaited()


async def test_unlimited_source_crosses_old_eight_mib_cap_in_batches(
    monkeypatch, tmp_path
):
    result, blobs, embedding, _ = await _source_call(
        monkeypatch,
        tmp_path,
        make_knowledge_preparation_budget("unlimited"),
        files=17,
        size=512 * 1024,
    )
    assert result == INDEX_ID and blobs.await_count == 17
    assert embedding.embed_documents.await_count == 9
    assert (
        max(len(call.args[0]) for call in embedding.embed_documents.await_args_list)
        == 2
    )


@pytest.mark.parametrize(
    "failure", [RuntimeError("embedding failed"), asyncio.CancelledError()]
)
async def test_source_spool_cleanup_on_failure_or_cancellation(
    monkeypatch, tmp_path, failure
):
    with pytest.raises(type(failure)) as caught:
        await _source_call(
            monkeypatch,
            tmp_path,
            make_knowledge_preparation_budget("small"),
            fail=failure,
        )
    assert caught.value is failure
    source.write_repository_index.assert_not_awaited()


def _history_item(number):
    return GitHubHistoryItem(
        repository=REPOSITORY,
        kind="issue",
        number=number,
        title="Cleanup",
        body=None,
        state="open",
        html_url=f"https://github.com/{REPOSITORY}/issues/{number}",
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        updated_at=datetime(2026, 10, 28, tzinfo=UTC),
    )


@pytest.mark.parametrize("limit,expected", [(None, [1, 2, 3]), (2, [1, 2])])
async def test_history_pages_deduplicate_and_apply_parent_budget(
    monkeypatch, limit, expected
):
    fetch = AsyncMock(
        side_effect=[
            GitHubHistoryPage((_history_item(1), _history_item(2)), 2),
            GitHubHistoryPage((_history_item(2), _history_item(3)), None),
        ]
    )
    stored = []

    async def prepare(*args, **kwargs):
        stored.extend(item.number for item in args[4].items)

    monkeypatch.setattr(history, "fetch_repository_history_page", fetch)
    monkeypatch.setattr(history, "_prepare_history_batch", prepare)
    result = await history.prepare_repository_history_pages(
        object(),
        object(),
        object(),
        REPOSITORY,
        embedding_provider_name="provider",
        embedding_model="model",
        embedding_dimensions=3,
        max_items=limit,
    )
    assert result == len(expected) and stored == expected
    assert fetch.await_count == (2 if limit is None else 1)


async def test_history_pages_reject_nonprogressing_pagination(monkeypatch):
    monkeypatch.setattr(
        history,
        "fetch_repository_history_page",
        AsyncMock(return_value=GitHubHistoryPage((_history_item(1),), 1)),
    )
    monkeypatch.setattr(history, "_prepare_history_batch", AsyncMock())
    with pytest.raises(ValueError, match="pagination"):
        await history.prepare_repository_history_pages(
            object(),
            object(),
            object(),
            REPOSITORY,
            embedding_provider_name="provider",
            embedding_model="model",
            embedding_dimensions=3,
            max_items=None,
        )


@pytest.mark.parametrize("host", ["api.github.com", "model.example"])
async def test_preparation_reuses_github_rate_limit_classification_only_for_github(
    monkeypatch, host
):
    from maintainer_lens.services.issue_investigator import GitHubRateLimitError

    request = httpx.Request("GET", f"https://{host}/resource")
    response = httpx.Response(429, request=request, headers={"retry-after": "60"})
    failure = httpx.HTTPStatusError("rate limited", request=request, response=response)
    prepare, _, followup = _preparer(monkeypatch, build=AsyncMock(side_effect=failure))
    with pytest.raises(
        GitHubRateLimitError if host == "api.github.com" else httpx.HTTPStatusError
    ) as caught:
        await prepare(REPOSITORY, make_knowledge_preparation_budget("small"))
    if host == "api.github.com":
        assert (
            caught.value.__cause__ is failure
            and caught.value.not_before.tzinfo is not None
        )
    else:
        assert caught.value is failure
    followup.assert_not_awaited()


async def test_real_preparation_scope_propagates_external_cancellation(monkeypatch):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def block(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    prepare, _, followup = _preparer(monkeypatch, build=block)
    operation = asyncio.create_task(
        prepare(REPOSITORY, make_knowledge_preparation_budget("unlimited"))
    )
    await asyncio.wait_for(entered.wait(), 1)
    operation.cancel("external")
    with pytest.raises(asyncio.CancelledError) as caught:
        await operation
    assert caught.value.args == ("external",) and cleaned.is_set()
    followup.assert_not_awaited()
