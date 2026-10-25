"""Evaluate history keyword and semantic retrieval on labeled GitHub issues."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from maintainer_lens.clients.embedding_factory import create_embedding_client
from maintainer_lens.clients.github import (
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
)
from maintainer_lens.clients.github_history import GitHubHistoryItem
from maintainer_lens.clients.github_http import create_github_http_client
from maintainer_lens.core.config import get_settings
from maintainer_lens.db.engine import create_db_engine
from maintainer_lens.db.models.repository_history import RepositoryHistoryRecord
from maintainer_lens.evaluation.retrieval_metrics import (
    RetrievalCase,
    evaluate_repository_retrieval,
)
from maintainer_lens.services.repository_history_collection import (
    RepositoryHistoryBatch,
)
from maintainer_lens.services.repository_history_indexing import (
    index_repository_history,
)
from maintainer_lens.services.repository_history_persistence import (
    save_repository_history_batch,
)
from maintainer_lens.services.repository_history_search import (
    search_repository_history,
)
from maintainer_lens.services.repository_history_semantic_search import (
    search_repository_history_semantic,
)

pytestmark = pytest.mark.anyio

REPOSITORY = "encode/httpx"
TOP_K = 5

ISSUE_NUMBERS = (
    832,
    1171,
    3712,
    3728,
    3734,
    3782,
)

EXPECTED_SOURCE_TERMS = {
    832: "Connect Timeout",
    1171: "PoolTimeout",
    3712: "MockTransport",
    3728: "cancel scope",
    3734: "create_ssl_context",
    3782: "double-cancel",
}


@dataclass(frozen=True, slots=True)
class LabeledHistoryQuery:
    """A manually labeled query with one relevant GitHub issue."""

    case_id: str
    query: str
    relevant_number: int


LABELED_QUERIES = (
    LabeledHistoryQuery(
        "connect-timeout",
        "Why does connecting time out when the origin server responds fast?",
        832,
    ),
    LabeledHistoryQuery(
        "pool-exhaustion",
        "Concurrent requests exceed connection limits and raise PoolTimeout",
        1171,
    ),
    LabeledHistoryQuery(
        "mock-elapsed",
        "Mock HTTP responses are missing the elapsed duration",
        3712,
    ),
    LabeledHistoryQuery(
        "cancel-scope",
        "AnyIO raises an error when exiting a cancellation scope",
        3728,
    ),
    LabeledHistoryQuery(
        "ssl-memory",
        "Creating SSL contexts repeatedly consumes excessive memory",
        3734,
    ),
    LabeledHistoryQuery(
        "double-cancellation",
        "Cancelling a streaming task twice leaves active connections in the pool",
        3782,
    ),
    LabeledHistoryQuery(
        "chinese-pool",
        "asyncio.gather 并发任务超过连接池限制时为什么会出现超时？",
        1171,
    ),
    LabeledHistoryQuery(
        "chinese-stream",
        "重复取消异步流式请求后，为什么连接池里的连接没有释放？",
        3782,
    ),
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def rollback_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Use real PostgreSQL while rolling back all evaluation writes."""

    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())

    try:
        async with engine.connect() as connection:
            outer_transaction = await connection.begin()

            session_factory = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )

            try:
                yield session_factory
            finally:
                if outer_transaction.is_active:
                    await outer_transaction.rollback()
    finally:
        await engine.dispose()


async def fetch_labeled_issues(
    github_client: httpx.AsyncClient,
) -> tuple[GitHubHistoryItem, ...]:
    """Read and verify the original labeled issues from GitHub."""

    items: list[GitHubHistoryItem] = []

    for number in ISSUE_NUMBERS:
        response = await github_client.get(
            f"{GITHUB_API_BASE_URL}/repos/{REPOSITORY}/issues/{number}",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        response.raise_for_status()

        payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError(f"Invalid GitHub issue #{number}")

        if "pull_request" in payload:
            raise ValueError(f"Expected an issue, got PR #{number}")

        item = GitHubHistoryItem.model_validate(
            {
                **payload,
                "repository": REPOSITORY,
                "kind": "issue",
            }
        )

        assert item.number == number
        assert str(item.html_url) == (
            f"https://github.com/{REPOSITORY}/issues/{number}"
        )

        source_text = f"{item.title}\n{item.body or ''}"

        assert EXPECTED_SOURCE_TERMS[number].casefold() in source_text.casefold(), (
            f"Ground truth needs review for issue #{number}"
        )

        items.append(item)

    return tuple(items)


def _keyword_keys(hits) -> tuple[str, ...]:
    """Map existing keyword hits to stable history source identities."""

    return tuple(
        (
            f"item:{hit.number}"
            if hit.record_type == "item"
            else f"comment:{hit.comment_id}"
        )
        for hit in hits
    )


def _semantic_keys(hits) -> tuple[str, ...]:
    """Map semantic hits to the same evidence identity format."""

    return tuple(
        (
            f"item:{hit.number}"
            if hit.source_type == "item"
            else f"comment:{hit.comment_id}"
        )
        for hit in hits
    )


def _rank(
    retrieved_keys: tuple[str, ...],
    relevant_key: str,
) -> int | None:
    """Return the one-based position of an expected result."""

    try:
        return retrieved_keys.index(relevant_key) + 1
    except ValueError:
        return None


async def test_live_history_retrieval_quality() -> None:
    if os.getenv("RUN_LIVE_HISTORY_EVALUATION") != "1":
        pytest.skip("Live history retrieval evaluation is disabled")

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration is disabled")

    settings = get_settings()

    provider_name = "openai-compatible"
    model_name = settings.embedding_model
    dimensions = settings.embedding_dimensions

    assert model_name
    assert dimensions > 0
    assert len(LABELED_QUERIES) == 8
    assert set(ISSUE_NUMBERS) == {case.relevant_number for case in LABELED_QUERIES}

    async with rollback_session_factory() as session_factory:
        # A pre-existing repository corpus would invalidate this small
        # controlled benchmark. Never delete existing user data.
        async with session_factory() as session:
            existing_count = await session.scalar(
                select(func.count())
                .select_from(RepositoryHistoryRecord)
                .where(RepositoryHistoryRecord.repository == REPOSITORY)
            )

        if existing_count:
            pytest.skip(
                "This benchmark requires a clean history corpus for "
                "encode/httpx. Use an isolated test database."
            )

        async with (
            create_github_http_client(settings.github_token) as github_client,
            httpx.AsyncClient(timeout=60.0) as embedding_http_client,
        ):
            embedding_client = create_embedding_client(
                embedding_http_client,
                settings,
            )

            issues = await fetch_labeled_issues(github_client)

            async with session_factory() as session:
                await save_repository_history_batch(
                    session,
                    REPOSITORY,
                    RepositoryHistoryBatch(
                        items=issues,
                        pages_fetched=1,
                        stop_reason="exhausted",
                    ),
                )

            indexing = await index_repository_history(
                session_factory,
                embedding_client,
                REPOSITORY,
                ISSUE_NUMBERS,
                embedding_provider_name=provider_name,
                embedding_model=model_name,
                embedding_dimensions=dimensions,
                max_comments=0,
                batch_size=min(
                    6,
                    settings.embedding_max_batch_size,
                ),
            )

            assert indexing.documents_found == len(ISSUE_NUMBERS)
            assert indexing.documents_embedded == len(ISSUE_NUMBERS)

            keyword_cases: list[RetrievalCase] = []
            semantic_cases: list[RetrievalCase] = []

            print(
                f"\nHistory benchmark: {REPOSITORY}, "
                f"{len(ISSUE_NUMBERS)} issues, "
                f"{len(LABELED_QUERIES)} queries, Top-{TOP_K}"
            )

            for case in LABELED_QUERIES:
                async with session_factory() as session:
                    keyword_hits = await search_repository_history(
                        session,
                        REPOSITORY,
                        case.query,
                        top_k=TOP_K,
                    )

                    semantic_hits = await search_repository_history_semantic(
                        session,
                        embedding_client,
                        REPOSITORY,
                        case.query,
                        embedding_provider_name=provider_name,
                        embedding_model=model_name,
                        embedding_dimensions=dimensions,
                        top_k=TOP_K,
                    )

                relevant_key = f"item:{case.relevant_number}"
                keyword_keys = _keyword_keys(keyword_hits)
                semantic_keys = _semantic_keys(semantic_hits)

                keyword_cases.append(
                    RetrievalCase(
                        query=case.query,
                        relevant_chunk_keys=frozenset({relevant_key}),
                        retrieved_chunk_keys=keyword_keys,
                    )
                )

                semantic_cases.append(
                    RetrievalCase(
                        query=case.query,
                        relevant_chunk_keys=frozenset({relevant_key}),
                        retrieved_chunk_keys=semantic_keys,
                    )
                )

                assert all(
                    hit.repository == REPOSITORY
                    for hit in (*keyword_hits, *semantic_hits)
                )

                print(
                    f"\n[{case.case_id}] {case.query}\n"
                    f"  expected: {relevant_key}\n"
                    f"  keyword rank: {_rank(keyword_keys, relevant_key)}\n"
                    f"  semantic rank: {_rank(semantic_keys, relevant_key)}\n"
                    f"  keyword results: {keyword_keys}\n"
                    f"  semantic results: {semantic_keys}"
                )

            keyword_summary = evaluate_repository_retrieval(
                keyword_cases,
                top_k=TOP_K,
            )
            semantic_summary = evaluate_repository_retrieval(
                semantic_cases,
                top_k=TOP_K,
            )

            print("\n=== History Retrieval Evaluation ===")
            print(f"Keyword Recall@{TOP_K}: {keyword_summary.mean_recall_at_k:.3f}")
            print(f"Keyword MRR@{TOP_K}: {keyword_summary.mrr_at_k:.3f}")
            print(f"Semantic Recall@{TOP_K}: {semantic_summary.mean_recall_at_k:.3f}")
            print(f"Semantic MRR@{TOP_K}: {semantic_summary.mrr_at_k:.3f}")

            assert keyword_summary.query_count == len(LABELED_QUERIES)
            assert semantic_summary.query_count == len(LABELED_QUERIES)


EXTENDED_QUERIES = LABELED_QUERIES + (
    LabeledHistoryQuery(
        "connect-timeout-paraphrase",
        "My API responds quickly yet the client fails while establishing a socket",
        832,
    ),
    LabeledHistoryQuery(
        "pool-exhaustion-paraphrase",
        "Requests pile up because every connection is occupied; how should I diagnose it?",
        1171,
    ),
    LabeledHistoryQuery(
        "mock-elapsed-chinese",
        "使用 MockTransport 模拟 HTTP 响应时为什么拿不到 elapsed 耗时？",
        3712,
    ),
    LabeledHistoryQuery(
        "cancel-scope-paraphrase",
        "Async cancellation context fails with a runtime error at exit",
        3728,
    ),
    LabeledHistoryQuery(
        "ssl-memory-paraphrase",
        "Why does opening new HTTPS sessions repeatedly lead to steadily growing memory?",
        3734,
    ),
    LabeledHistoryQuery(
        "double-cancellation-paraphrase",
        "Cancel a streaming HTTP task, then cancel it again and connections remain checked out",
        3782,
    ),
)


async def fetch_distractor_issues(
    github_client: httpx.AsyncClient,
    *,
    count: int = 34,
) -> tuple[GitHubHistoryItem, ...]:
    """Collect additional real GitHub issues for a larger retrieval corpus."""

    if type(count) is not int or not 1 <= count <= 100:
        raise ValueError("count must be between 1 and 100")

    distractors: list[GitHubHistoryItem] = []
    seen_numbers = set(ISSUE_NUMBERS)

    for page_number in range(1, 6):
        response = await github_client.get(
            f"{GITHUB_API_BASE_URL}/repos/{REPOSITORY}/issues",
            params={
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 100,
                "page": page_number,
            },
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )

        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, list):
            raise ValueError("Expected a GitHub issue list")

        for raw in payload:
            if not isinstance(raw, dict):
                raise ValueError("Invalid GitHub issue entry")

            # GitHub's Issues API also returns pull requests.
            if "pull_request" in raw:
                continue

            number = raw.get("number")

            if type(number) is not int or number <= 0:
                raise ValueError("Invalid GitHub issue number")

            if number in seen_numbers:
                continue

            item = GitHubHistoryItem.model_validate(
                {
                    **raw,
                    "repository": REPOSITORY,
                    "kind": "issue",
                }
            )

            if str(item.html_url) != (
                f"https://github.com/{REPOSITORY}/issues/{number}"
            ):
                raise ValueError("Unexpected GitHub issue URL")

            seen_numbers.add(number)
            distractors.append(item)

            if len(distractors) == count:
                return tuple(distractors)

        if "next" not in response.links:
            break

    raise RuntimeError(
        f"Only collected {len(distractors)} of {count} required distractor issues"
    )


async def test_live_history_retrieval_quality_extended() -> None:
    """Evaluate keyword and semantic retrieval against forty real issues."""

    if os.getenv("RUN_LIVE_HISTORY_EVALUATION") != "1":
        pytest.skip("Live history retrieval evaluation is disabled")

    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration is disabled")

    settings = get_settings()

    provider_name = "openai-compatible"
    model_name = settings.embedding_model
    dimensions = settings.embedding_dimensions

    assert model_name
    assert dimensions > 0
    assert len(EXTENDED_QUERIES) == 14

    async with rollback_session_factory() as session_factory:
        async with session_factory() as session:
            existing_count = await session.scalar(
                select(func.count())
                .select_from(RepositoryHistoryRecord)
                .where(RepositoryHistoryRecord.repository == REPOSITORY)
            )

        if existing_count:
            pytest.skip(
                "Extended benchmark requires a clean history corpus "
                "for encode/httpx. Use an isolated test database."
            )

        async with (
            create_github_http_client(settings.github_token) as github_client,
            httpx.AsyncClient(timeout=60.0) as embedding_http_client,
        ):
            embedding_client = create_embedding_client(
                embedding_http_client,
                settings,
            )

            # Retrieve six labeled issues and thirty-four additional issues.
            labeled_issues = await fetch_labeled_issues(github_client)
            distractors = await fetch_distractor_issues(
                github_client,
                count=34,
            )

            corpus = labeled_issues + distractors
            corpus_numbers = tuple(item.number for item in corpus)
            corpus_keys = {f"item:{number}" for number in corpus_numbers}

            assert len(corpus) == 40
            assert len(set(corpus_numbers)) == 40
            assert set(ISSUE_NUMBERS).issubset(corpus_numbers)

            print(
                "\nExtended history evaluation corpus:",
                tuple(sorted(corpus_numbers)),
            )

            # Persist the controlled corpus, without changing sync cursors.
            async with session_factory() as session:
                await save_repository_history_batch(
                    session,
                    REPOSITORY,
                    RepositoryHistoryBatch(
                        items=corpus,
                        pages_fetched=1,
                        stop_reason="page_limit",
                    ),
                )

            # Task 49 intentionally indexes at most ten parent issues per call.
            total_embedded = 0

            for offset in range(0, len(corpus_numbers), 10):
                number_batch = corpus_numbers[offset : offset + 10]

                result = await index_repository_history(
                    session_factory,
                    embedding_client,
                    REPOSITORY,
                    number_batch,
                    embedding_provider_name=provider_name,
                    embedding_model=model_name,
                    embedding_dimensions=dimensions,
                    max_comments=0,
                    batch_size=min(
                        6,
                        settings.embedding_max_batch_size,
                    ),
                )

                assert result.documents_found == len(number_batch)
                total_embedded += result.documents_embedded

            assert total_embedded == 40

            keyword_cases: list[RetrievalCase] = []
            semantic_cases: list[RetrievalCase] = []

            keyword_top1 = 0
            semantic_top1 = 0
            keyword_top3 = 0
            semantic_top3 = 0

            print(
                f"\nHistory quality evaluation: "
                f"{len(corpus)} issues, "
                f"{len(EXTENDED_QUERIES)} queries, Top-{TOP_K}"
            )

            for case in EXTENDED_QUERIES:
                async with session_factory() as session:
                    keyword_hits = await search_repository_history(
                        session,
                        REPOSITORY,
                        case.query,
                        top_k=TOP_K,
                    )

                    semantic_hits = await search_repository_history_semantic(
                        session,
                        embedding_client,
                        REPOSITORY,
                        case.query,
                        embedding_provider_name=provider_name,
                        embedding_model=model_name,
                        embedding_dimensions=dimensions,
                        top_k=TOP_K,
                    )

                relevant_key = f"item:{case.relevant_number}"

                keyword_keys = _keyword_keys(keyword_hits)
                semantic_keys = _semantic_keys(semantic_hits)

                # All search results must belong to the controlled corpus.
                assert set(keyword_keys).issubset(corpus_keys)
                assert set(semantic_keys).issubset(corpus_keys)

                keyword_rank = _rank(keyword_keys, relevant_key)
                semantic_rank = _rank(semantic_keys, relevant_key)

                keyword_top1 += keyword_rank == 1
                semantic_top1 += semantic_rank == 1

                keyword_top3 += keyword_rank is not None and keyword_rank <= 3
                semantic_top3 += semantic_rank is not None and semantic_rank <= 3

                keyword_cases.append(
                    RetrievalCase(
                        query=case.query,
                        relevant_chunk_keys=frozenset({relevant_key}),
                        retrieved_chunk_keys=keyword_keys,
                    )
                )

                semantic_cases.append(
                    RetrievalCase(
                        query=case.query,
                        relevant_chunk_keys=frozenset({relevant_key}),
                        retrieved_chunk_keys=semantic_keys,
                    )
                )

                print(
                    f"\n[{case.case_id}] {case.query}\n"
                    f"  expected: {relevant_key}\n"
                    f"  keyword rank: {keyword_rank}\n"
                    f"  semantic rank: {semantic_rank}\n"
                    f"  keyword results: {keyword_keys}\n"
                    f"  semantic results: {semantic_keys}"
                )

            keyword_summary = evaluate_repository_retrieval(
                keyword_cases,
                top_k=TOP_K,
            )
            semantic_summary = evaluate_repository_retrieval(
                semantic_cases,
                top_k=TOP_K,
            )

            total_queries = len(EXTENDED_QUERIES)

            assert keyword_summary.query_count == total_queries
            assert semantic_summary.query_count == total_queries

            print("\n=== Extended History Retrieval Evaluation ===")
            print(f"Corpus size: {len(corpus)}")
            print(f"Query count: {total_queries}")

            print(f"Keyword Top-1: {keyword_top1 / total_queries:.3f}")
            print(f"Semantic Top-1: {semantic_top1 / total_queries:.3f}")
            print(f"Keyword Top-3: {keyword_top3 / total_queries:.3f}")
            print(f"Semantic Top-3: {semantic_top3 / total_queries:.3f}")

            print(f"Keyword Recall@{TOP_K}: {keyword_summary.mean_recall_at_k:.3f}")
            print(f"Keyword MRR@{TOP_K}: {keyword_summary.mrr_at_k:.3f}")
            print(f"Semantic Recall@{TOP_K}: {semantic_summary.mean_recall_at_k:.3f}")
            print(f"Semantic MRR@{TOP_K}: {semantic_summary.mrr_at_k:.3f}")
