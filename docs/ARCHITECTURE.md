# MaintainerLens Architecture

> Reviewed against local source and Git state on 2026-10-28. The developer confirmed the default pytest suite and the focused PostgreSQL maintenance regressions passed after the Session-mock fixture correction. The assistant did not run those tests. Current remote CI, deployed code, service health and applied database revisions remain unverified. Implementation, local verification, merge and deployment are distinct states.

Use this document as a navigation map: select the relevant scenario below, read its entry point, callers and focused tests, then verify current signatures and Git changes. A new conversation normally needs a scoped read of those modules rather than another repository-wide audit. [Roadmap](ROADMAP.md) tracks planned milestones; [ADRs](ADR/README.md) explain architectural decisions. Update the relevant navigation row when changing an entry point or its contract.

## 1. System Overview

MaintainerLens is an asynchronous, evidence-grounded GitHub Issue investigation service for public repositories. V1 provides read-only server-side investigation, not IDE editing or automatic patches. Its current architecture has three implementation boundaries:

| Boundary | Responsibilities | Current integration |
| --- | --- | --- |
| Reliable task backend (M0–M2) | HTTP intake/readback, PostgreSQL tasks and Outbox, confirmed RabbitMQ publication, Worker execution, claims/leases, fenced Issue snapshots | Connected through the production entry points; the processor currently fetches and saves an Issue snapshot |
| Knowledge services (M3) | Versioned source/document indexing, history collection/sync/enrichment, embeddings, retrieval and evaluation | Independently callable; Graph adapters reuse retrieval services; the production snapshot processor does not prepare or search knowledge |
| Investigation workflow (M4) | Typed State, evidence conversion, structured analysis, bounded follow-up planning/tool calling, optional tool-step controls | Independently callable and locally tested; not connected to the production Worker |

PostgreSQL owns business task state and persisted evidence. RabbitMQ transports at-least-once work signals. The existing `src/maintainer_lens/investigation_runner.py` owns task execution and leases; LangGraph owns investigation node/edge orchestration. These responsibilities must remain distinct.

The API, Outbox Dispatcher and Worker are independent long-running Python processes. Compose also defines PostgreSQL 17/pgvector, Redis 7.4 and RabbitMQ 4.2, plus a one-shot Alembic migration service. Redis currently has infrastructure/lifecycle integration; it is not an implemented Agent memory, retrieval cache or distributed rate coordinator.

**Not implemented:** Worker-to-Graph wiring, a durable investigation report/schema/readback, a whole-Graph application deadline, LangGraph checkpoint recovery, HITL, cross-investigation memory, multi-role Agent orchestration, private-repository authorization or approved GitHub writes. `draft_preliminary_report` is a routing marker, not a report generator.

## 2. Module Navigation Map

Paths in the tables link to actual source/test files. Source paths are under `src/maintainer_lens/`; tests are under `tests/`. Search anchors are identifiers for targeted `rg` searches, not additional APIs. Read the focused tests before changing semantics; opt-in and mocked coverage are distinguished in Section 5.

### Task backend and infrastructure

| Change scenario | Primary entry points / symbols | Dependencies, callers and boundaries | Focused tests | Search anchors |
| --- | --- | --- | --- | --- |
| Submit an investigation or change HTTP readback | [API routes](../src/maintainer_lens/api/routes/investigations.py): `create_investigation`, `get_investigation`; [submission](../src/maintainer_lens/services/investigation_submission.py): `create_investigation_submission` | HTTP [schemas](../src/maintainer_lens/schemas/investigation_api.py); [detail query](../src/maintainer_lens/db/investigation_queries.py): `get_investigation_with_snapshot`. Task + Outbox commit together; GET currently exposes a snapshot, not a report | [API](../tests/api/test_investigations.py), [submission atomicity](../tests/integration/test_investigation_submission.py) | `CreateInvestigationRequest`, `InvestigationDetailResponse` |
| Deliver or recover Outbox events | [process](../src/maintainer_lens/outbox_dispatcher.py): `run_dispatcher_loop`; [service](../src/maintainer_lens/services/outbox_dispatcher.py): `dispatch_one_outbox_event` | [DB Outbox](../src/maintainer_lens/db/outbox.py): claim/token/lease and publication marking; publisher confirms precede DB marking; network publication holds no DB transaction | [Outbox ownership](../tests/integration/test_outbox.py), [Dispatcher](../tests/integration/test_outbox_dispatcher.py) | `claim_next_outbox_event`, `mark_outbox_published`, `delivery_token` |
| Change delivery, retries, poison messages or ACK behavior | [handler](../src/maintainer_lens/messaging/handler.py): `handle_investigation_message_with_retry`, `process_investigation_request`; [consumer](../src/maintainer_lens/messaging/consumer.py): `consume_investigations` | [topology](../src/maintainer_lens/messaging/topology.py), [message schema](../src/maintainer_lens/messaging/schemas.py), [publisher](../src/maintainer_lens/messaging/publisher.py). Handler routes; Consumer alone ACKs after handler return | [routing](../tests/messaging/test_handler_retry.py), [deadline](../tests/messaging/test_retry_deadline.py), [ACK safety](../tests/messaging/test_delivery_safety.py), [broker E2E](../tests/integration/test_worker_e2e.py) | `ml-retry-attempts`, `ml-retry-not-before`, `InvalidInvestigationMessageError` |
| Change task ownership, processor errors or cancellation | [runner](../src/maintainer_lens/investigation_runner.py): `execute_investigation_task`, `run_investigation_with_lease`, `maintain_investigation_lease`; [task DB](../src/maintainer_lens/db/investigation_tasks.py) | Worker/handler inject `processor(claim_token)`; runner owns fenced status transitions. Processor error classification is separate from heartbeat/finalization failures | [classification](../tests/test_investigation_error_classification.py), [TaskGroup](../tests/test_worker_taskgroup.py), [real DB recovery](../tests/integration/test_investigation_error_recovery.py), [task execution](../tests/integration/test_worker_execution.py) | `claim_token`, `LeaseLostError`, `_classify_processing_error` |
| Change Issue fetching or snapshot writes | [investigator](../src/maintainer_lens/services/issue_investigator.py): `investigate_issue`; [snapshot DB](../src/maintainer_lens/db/issue_snapshots.py): `upsert_issue_snapshot` | [Worker](../src/maintainer_lens/worker.py): `run_worker`, `build_investigation_message_handler`; GitHub client; snapshot commit precedes task success in another transaction | [GitHub errors](../tests/services/test_issue_investigator_errors.py), [snapshot fencing](../tests/integration/test_issue_snapshots.py), [production wiring](../tests/test_worker_production_wiring.py) | `GitHubRateLimitError`, `IssueSnapshotData`, `production_investigate` |
| Change startup, settings or resource cleanup | [API lifespan](../src/maintainer_lens/main.py): `lifespan`; [Worker](../src/maintainer_lens/worker.py): `run_worker`; [settings](../src/maintainer_lens/core/config.py): `Settings`, `get_settings` | `AsyncExitStack`, [Session factory](../src/maintainer_lens/db/session.py): `create_session_factory`, independent HTTP/MQ resources; [Compose](../compose.yaml), [CI](../.github/workflows/ci.yml) | [lifespan](../tests/api/test_lifespan.py), [Worker startup](../tests/test_worker_startup.py), [settings](../tests/test_config.py) | `push_async_callback`, `expire_on_commit`, `publisher_confirms` |

### Knowledge, retrieval and evaluation

| Change scenario | Primary entry points / symbols | Dependencies, callers and boundaries | Focused tests | Search anchors |
| --- | --- | --- | --- | --- |
| Change GitHub access or public-repository enforcement | [GitHub client](../src/maintainer_lens/clients/github.py): `fetch_public_repository_metadata`, `fetch_github_issue`; [revision resolver](../src/maintainer_lens/clients/github_repository.py): `resolve_repository_revision` | History page/detail clients share the metadata guard. Tree/blob readers consume backend-selected revisions/manifests. Optional PAT changes authentication, not V1 public scope | [public policy](../tests/clients/test_public_repository_policy.py), [Issue client](../tests/clients/test_github.py), [revision client](../tests/clients/test_github_repository.py) | `private`, `fetch_repository_history_page`, `fetch_pull_request_details` |
| Change source selection, download or chunking | [manifest](../src/maintainer_lens/services/repository_ingestion.py): `build_repository_manifest`; [download](../src/maintainer_lens/services/repository_sources.py): `download_repository_sources`; [chunker](../src/maintainer_lens/services/source_chunker.py): `chunk_repository_source` | GitHub Tree/Blob clients validate immutable content; [format policy](../src/maintainer_lens/core/source_types.py), [syntax registry](../src/maintainer_lens/services/syntax_registry.py). Language-specific analyzers stay separate from chunk-window policy | [ingestion](../tests/services/test_repository_ingestion.py), [Blob integrity](../tests/clients/test_github_blob.py), [download limits](../tests/services/test_repository_sources.py), [adaptive chunks](../tests/services/test_adaptive_chunker.py) | `fetch_repository_manifest`, `fetch_repository_blob`, `SYNTAX_ANALYZERS` |
| Change Embedding provider/configuration | [client](../src/maintainer_lens/clients/embedding.py): `EmbeddingProvider`, `OpenAICompatibleEmbeddingClient`; [factory](../src/maintainer_lens/clients/embedding_factory.py): `create_embedding_client` | Source/history indexers and query retrievers share the protocol. Preserve provider/model/dimension compatibility and input-version semantics; caller owns HTTP client lifetime | [client](../tests/clients/test_embedding.py), [factory](../tests/clients/test_embedding_factory.py) | `embed_documents`, `embed_query`, `embedding_dimensions` |
| Change source-index reuse, build concurrency or publication | [indexer](../src/maintainer_lens/services/repository_indexing.py): `build_repository_index`; [writer](../src/maintainer_lens/services/repository_index_writer.py): `write_repository_index`, `make_chunk_key` | [build lease](../src/maintainer_lens/services/repository_index_lease.py), [ORM](../src/maintainer_lens/db/models/repository_index.py). Build ownership differs from task ownership; chunks and ready status publish atomically | [build lease lifecycle](../tests/services/test_repository_indexing_lease.py), [ready reuse](../tests/services/test_repository_index_precheck.py), [writer fencing](../tests/integration/test_repository_index_writer_fencing_integration.py) | `RepositoryIndexBuildInProgress`, `owner_token`, `uq_repository_indexes_identity` |
| Change Source ranking, exact matches or returned metadata | [search](../src/maintainer_lens/services/repository_search.py): `search_repository`; [hybrid](../src/maintainer_lens/services/repository_hybrid_search.py): `search_repository_hybrid`, `HybridSearchResult` | Query analysis → semantic/vector + optional keyword → RRF. Underlying services own sequential short transactions; Graph source adapter calls this existing entry point | [automatic search](../tests/services/test_repository_search.py), [hybrid](../tests/services/test_repository_hybrid_search.py), [pgvector](../tests/integration/test_repository_vector_search_integration.py) | `analyze_repository_query`, `search_repository_by_text`, `fuse_repository_rankings` |
| Change History collection, incremental sync or backfill | [sync](../src/maintainer_lens/services/repository_history_sync.py): `sync_repository_history`; [backfill](../src/maintainer_lens/services/repository_history_backfill.py): `backfill_repository_history` | [bounded collection](../src/maintainer_lens/services/repository_history_collection.py) and [persistence](../src/maintainer_lens/services/repository_history_persistence.py). Sync watermark and backfill cursor differ; neither is a LangGraph checkpoint | [collection](../tests/services/test_repository_history_collection.py), [sync](../tests/integration/test_repository_history_sync.py), [backfill](../tests/integration/test_repository_history_backfill.py) | `completed_through`, `requested_since`, `RepositoryHistoryBackfillConflict` |
| Change History details, embedding reuse or preparation | [enrichment](../src/maintainer_lens/services/repository_history_enrichment.py): `collect_repository_history_enrichment`, `save_repository_history_enrichment`; [indexer](../src/maintainer_lens/services/repository_history_indexing.py): `index_repository_history` | [small pipeline](../src/maintainer_lens/services/repository_history_pipeline.py): `prepare_repository_history`; parent records precede dependent details; no automatic embedding-refresh scheduler or sync-cursor advancement in this pipeline | [enrichment](../tests/integration/test_repository_history_enrichment.py), [reuse/isolation](../tests/integration/test_repository_history_semantic_search_integration.py), [pipeline](../tests/integration/test_repository_history_pipeline_integration.py) | `content_sha256`, `MAX_CONTENT_CHARS`, `documents_reused` |
| Change History retrieval or provenance fields | [semantic](../src/maintainer_lens/services/repository_history_semantic_search.py): `search_repository_history_semantic`, `RepositoryHistorySemanticHit`; [keyword](../src/maintainer_lens/services/repository_history_search.py): `search_repository_history` | Semantic reads embedded content snapshots scoped by repo/provider/model/dimensions; keyword searches stored parents/comments. Graph adapters default to semantic | [semantic](../tests/integration/test_repository_history_semantic_search_integration.py), [keyword](../tests/integration/test_repository_history_search_integration.py) | `source_key`, `source_updated_at`, `cosine_distance` |
| Change quality datasets, evidence anchors or metrics | [metrics](../src/maintainer_lens/evaluation/retrieval_metrics.py): `evaluate_repository_retrieval`; [comparison](../src/maintainer_lens/evaluation/retrieval_comparison.py): `compare_repository_retrieval` | Dataset loader and anchor resolution under the same package; [pinned source labels](../tests/fixtures/retrieval/sampleproject.json). Live evaluation requires explicit external-service opt-in | [pure evaluation](../tests/evaluation/), [DB anchor resolution](../tests/integration/test_retrieval_anchor_resolution_postgres.py), [live History benchmark](../tests/integration/test_repository_history_retrieval_quality_live.py) | `Recall@K`, `mrr_at_k`, `resolve_dataset_chunk_keys`, `EXTENDED_QUERIES` |

### Investigation workflow and persistence contracts

| Change scenario | Primary entry points / symbols | Dependencies, callers and boundaries | Focused tests | Search anchors |
| --- | --- | --- | --- | --- |
| Change Graph state, edges, rounds or stopping rules | [composer](../src/maintainer_lens/agents/investigation_graph.py): `build_investigation_graph`; [State](../src/maintainer_lens/agents/investigation/state.py): `InvestigationState`; [routing](../src/maintainer_lens/agents/investigation/routing.py): `make_analysis_router` | Injected searchers/analyzer; composer stays thin. Query Planner and tool strategy are mutually exclusive. Production Worker is not a caller | [routing](../tests/agents/test_investigation_graph_routing.py), [bounded loop](../tests/agents/test_investigation_graph_loop.py) | `max_search_rounds`, `no_new_evidence`, `next_step` |
| Change Graph search adapters or Session ownership | [adapters](../src/maintainer_lens/agents/investigation/retrieval.py): `create_history_searcher`, `create_source_searcher`, search-node factories | Bind session factory/Embedding dependencies to `HistorySearcher` and `SourceSearcher`; Source ownership lookup must finish before M3 retrieval starts | [source adapter unit](../tests/agents/test_investigation_graph_source.py), [real Graph/pgvector regression](../tests/integration/test_investigation_source_searcher_postgres.py) | `session.begin`, `source_index_id`, `SourceSearcher` |
| Change evidence identities, citations or structured analysis | [evidence](../src/maintainer_lens/agents/investigation/evidence.py): conversion/merge functions, `history_evidence_id`; [analysis node](../src/maintainer_lens/agents/investigation/analysis_node.py): `make_analysis_node`; [analyzer](../src/maintainer_lens/agents/investigation_analysis.py): `InvestigationAnalysis`, `create_llm_analyzer` | Validate provenance before context construction; findings cite only IDs actually supplied to that analysis. Self-issue evidence is excluded; model feedback is not a validated finding | [analysis/citations](../tests/agents/test_investigation_graph_analysis.py), [merges](../tests/agents/test_investigation_evidence.py), [safety](../tests/agents/test_investigation_safety.py) | `allowed_ids`, `preliminary_analysis`, `evidence_ids` |
| Change LLM follow-up query planning | [planner](../src/maintainer_lens/agents/investigation/query_planning.py): `create_llm_query_planner`, `make_query_planning_node`, `apply_planned_query` | Uses prior queries/analysis gaps; rejects empty, oversized or repeated queries; returns to fixed RAG nodes. Does not execute function calling | [planner](../tests/agents/test_investigation_query_planning.py) | `FollowUpQueryPlan`, `normalize_query`, `planned_query` |
| Change model-selected tools, feedback or step timeout | [protocol](../src/maintainer_lens/agents/investigation/tool_calling.py): `run_search_tool_step`, `SearchToolArguments`; [adapter](../src/maintainer_lens/agents/investigation/tool_node.py): `create_tool_search_runner`, `make_tool_search_node`; [controls](../src/maintainer_lens/agents/investigation/execution.py): `with_tool_step_controls` | Backend fixes repo/index; exactly one allowed search per step. Merge selected-tool evidence directly; controls must be explicitly wrapped by caller | [protocol](../tests/agents/test_investigation_tool_calling.py), [compiled Graph](../tests/agents/test_investigation_graph_tool.py), [timeout/cancellation/logs](../tests/agents/test_investigation_execution.py) | `tool_call_id`, `tool_choice`, `last_tool_feedback`, `investigation_tool_step` |
| Change persisted task/knowledge shape or design future reports | [task ORM](../src/maintainer_lens/db/models/investigation.py); [source ORM](../src/maintainer_lens/db/models/repository_index.py); [History ORM](../src/maintainer_lens/db/models/repository_history_embedding.py); [migrations](../migrations/versions/) | Check API schemas, guarded writers and [Alembic metadata](../migrations/env.py) together. There is currently no report ORM/migration; graph state is not task status | [task transitions](../tests/integration/test_investigation_task_transitions.py), [source schema](../tests/integration/test_repository_index_schema.py), [History persistence](../tests/integration/test_repository_history_persistence.py) | `InvestigationTask`, `InvestigationIssueSnapshot`, `RepositoryIndex`, `RepositoryHistoryEmbedding` |

Example scoped lookup from the repository root:

```bash
rg -n 'create_source_searcher|search_repository_by_text|session.begin' \
  src/maintainer_lens/agents/investigation/retrieval.py \
  src/maintainer_lens/services/repository_semantic_search.py \
  tests/integration/test_investigation_source_searcher_postgres.py
```

## 3. Critical Execution Flows

### 3.1 Production HTTP → Outbox → RabbitMQ → Worker → snapshot

1. `POST /investigations` validates repository syntax and a positive issue number. `create_investigation_submission()` creates `InvestigationTask` and `InvestigationOutboxEvent` in one transaction; it returns a typed message only after commit. HTTP responds 202. Public visibility is checked later by the GitHub client, not during submission.
2. The independent dispatcher calls `dispatch_one_outbox_event()`. `claim_next_outbox_event()` uses `FOR UPDATE SKIP LOCKED`, a delivery token and a five-minute lease, then commits the claim.
3. Outside a DB transaction, the dispatcher validates the event payload and publishes a persistent, mandatory-routed message over a publisher-confirm channel, with a 30-second publish deadline. It then marks publication in a new token/lease-guarded transaction. Failed publication schedules a retry (currently 60 seconds). A crash after broker confirmation but before DB marking can duplicate delivery.
4. `run_worker()` assembles resources and the production `investigate_issue()` callback. Its consumer uses `prefetch_count=1`, sequential iteration and a channel separate from confirmed republishing.
5. `consume_investigations()` awaits the handler. `build_investigation_message_handler()` connects `handle_investigation_message_with_retry()` → `process_investigation_request()` → `execute_investigation_task()`.
6. The runner commits an atomic task claim and passes its new claim token to the processor. `TaskGroup` coordinates processing and periodic lease renewal through separate sessions.
7. `investigate_issue()` calls the GitHub client, which verifies public repository metadata and validates the requested Issue identity (rejecting PR objects). No DB transaction spans these requests. The service maps rate limits/transient failures and constructs `IssueSnapshotData`.
8. `upsert_issue_snapshot()` locks/validates task ownership and upserts by task ID in one transaction. The runner subsequently commits `succeeded` in another guarded transaction. `GET /investigations/{task_id}` reads task + optional snapshot through an outer join.
9. The handler returns only after successful processing or confirmed retry/dead-letter publication. Consumer then ACKs. An ACK can mean successful routing of a failed attempt, not task success.

**There is no call from this production chain into `build_investigation_graph()` or Graph `ainvoke()`.** The future connection is described in Section 6, not represented as an existing edge.

### 3.2 Source indexing and retrieval

```text
build_repository_index
  → resolve public default branch → pinned commit/tree
  → ready-index lookup → build claim → ready-index recheck
  → Tree manifest → bounded verified Blob downloads → adaptive chunks
  → ordered embedding batches (no long DB transaction)
  → token-fenced writer transaction → chunks + ready index → release build lease

search_repository
  → conservative code-identifier analysis
  → search_repository_hybrid
      → search_repository_by_text → query embedding → exact pgvector Top-K
      → optional escaped ILIKE keyword search
      → RRF → HybridSearchResult with provenance and ranks
```

The index identity is repo + commit + Embedding provider/model/dimensions + chunker version. `make_chunk_key()` hashes file/Blob identity, content, line span and structural metadata. A matching ready index avoids rebuilding/embedding; a concurrent build reports `RepositoryIndexBuildInProgress`. Build heartbeats and task heartbeats have different owners and tokens.

Tree retrieval falls back from truncated recursive responses to bounded subtree traversal. Blob fetching verifies SHA, Base64, declared/actual size, recomputed Git Blob identity and UTF-8 text. Current download limits include 512 KiB per selected file, 8 MiB per batch and at most eight download workers. The format allowlist is broader than the syntax-analyzer set: Python/JS/TS/Go/Java use syntax hints, Markdown uses headings, and other allowed text/config formats use window fallback.

Semantic search uses exact cosine distance; keyword search uses literal escaped `ILIKE`, not BM25. Semantic and optional keyword queries execute sequentially on one Session, then RRF fuses ranks (default constant 60). V1 has no HNSW, IVFFlat, reranker or separate search engine.

### 3.3 History preparation and retrieval

```text
collect_repository_history → write/save_repository_history_batch
sync_repository_history   → bounded updated-since scan → data + watermark commit
backfill_repository_history → created-order pages → data + guarded cursor commit

stored parent Issue/PR
  → bounded comments/PR metadata enrichment → detail persistence
  → index_repository_history → embed changed selected records → embedding upsert
  → semantic search (default) or keyword search
```

These are separate callable operations. Incremental sync uses a default one-day overlap and advances `completed_through` only when pagination is exhausted. Backfill locks and checks its cursor before committing records/progress. Those checkpoints represent ingestion progress, not LangGraph state.

`prepare_repository_history()` composes a small collect/enrich/index batch without advancing sync cursors. Parent rows precede dependent details; monotonic source timestamps prevent older ingestion from overwriting newer data. PR enrichment stores merge/head/base revisions and changed-file counts, not diffs or verified Issue-to-fix relationships.

History embedding identity is repo + item/comment source key + provider/model/dimensions. Bodies are truncated to 5,000 characters; content hash/timestamp matching controls reuse. Indexing closes its read session before external embedding calls. Sync/enrichment does not automatically refresh embeddings. Semantic retrieval returns the embedded text snapshot, source URL and update time; keyword retrieval searches stored parent/comment text. No automatic full-history scheduler or refresh service is implied.

### 3.4 Independently callable LangGraph workflow

```text
prepare_query → search_history → optional search_source → analyze_evidence
                                                       │
                  sufficient cited findings, no gaps ──┴→ ready marker → END
                  otherwise, budget and evidence permit follow-up:
                    deterministic query OR LLM query planner
                      → fixed History/optional Source nodes → merge → analysis
                    OR one LLM-selected search tool
                      → selected retriever only → merge
                      → novel evidence: analysis; duplicate/no novelty: stop
                  exhausted budget / no useful query / no evidence → needs-more marker → END
```

`InvestigationState` holds explicit Issue context, language, evidence, rounds, prior queries, analysis and routing fields. The compiled Graph supports retrieval-only operation without an analyzer. Search rounds default to one and are restricted to 1–3. Planner and tool selection cannot both be configured. First-round empty retrieval stops without a model analysis request; no novel follow-up evidence skips repeat analysis.

The analyzer validates `summary`, cited `findings`, `hypotheses` and `unanswered_questions`. Each analysis sees at most five History and five Source excerpts (1,600 content characters each); citation membership is checked against that exact input, not all accumulated State. Source URLs pin commit/path/lines; History URLs must match repo/object/comment identities. The investigated Issue and its comments are excluded from independent historical evidence. Legal IDs and URLs do not prove that a natural-language finding is supported.

Tool calling validates one whitelisted `search_history`/`search_source` call and a strict bounded query. Repo/index/credentials remain backend-controlled. It sends the same `tool_call_id` back through `role=tool`, then requests final feedback with further tool calls disabled. The selected top-five records retain complete fields in State; excerpts sent to the model are bounded. `last_tool_feedback` is free text, not a verified finding. Query Planner instead proposes a query and returns to the fixed retrieval nodes.

Tool selection/feedback caps are 600/500 output tokens; analysis and query planning also have per-request caps. A three-round tool workflow can make up to seven Chat requests plus embeddings. `with_tool_step_controls()` is explicitly applied by the caller (default 120 seconds), propagates cancellation and distinguishes its own deadline from upstream timeouts. Its logs contain execution metadata, not Issue/model/error-message content. It is not an entire-investigation deadline, a monetary budget or a guarantee that cancellation stops remote billing.

## 4. Critical Boundaries

### 4.1 Transaction ownership and Sessions

| Operation | Transaction / resource owner | Invariant |
| --- | --- | --- |
| HTTP submission | `create_investigation_submission()` | Task and Outbox commit or roll back together; request does not directly publish |
| Outbox delivery | Dispatcher service + DB Outbox helpers | Claim transaction → confirmed network publish without transaction → guarded mark/retry transaction |
| Task lifecycle | Existing runner + task DB helpers | Claim commits before processor; heartbeat and terminal transitions use independent short sessions/transactions |
| Snapshot | Investigator service transaction + fenced DB writer | Required snapshot commit precedes task success; snapshot and success are not atomic together |
| Source build | Indexer, independent lease sessions, writer transaction | Network/embedding work outside publication transaction; writer locks active build lease before atomically publishing chunks/ready |
| Graph Source adapter | `create_source_searcher()` and existing M3 services | Explicit ownership-check transaction must end before calling retrieval; downstream services own their own transactions |
| History ingestion/indexing | Respective sync/backfill/enrichment/index services | Data/cursor consistency and monotonic updates; release read resources before embedding; no shared concurrent Session |

**Source Searcher regression:** `session.get()` can auto-begin a transaction. Previously the adapter called it without ending that transaction, then passed the same Session into `search_repository_by_text()`, which calls `session.begin()`. The repaired adapter puts the lookup and repo check inside an explicit short `session.begin()`, exits it, and only then invokes `search_repository()`. Do not wrap that entire call in the lookup transaction or use nested savepoints to hide conflicting transaction ownership. The real Graph/pgvector regression checks no active Session transaction during fake embeddings, errors, retries, rejection paths and cancellation cleanup.

Share engines, session factories and suitably managed HTTP clients; give concurrent DB tasks separate Sessions. `create_session_factory()` uses `expire_on_commit=False`. Production entry points use `AsyncExitStack`/lifespan cleanup so partial startup and cancellation release acquired resources.

### 4.2 Claims, leases, idempotency and ACK

Task SQL atomically claims `pending` or expired `running` tasks with a fresh token. The lease is 15 minutes and default renewal interval is five minutes. Success/failure/release/renewal check task ID, status, token and PostgreSQL time. Task state SQL uses `statement_timestamp()`; Snapshot/Outbox checks use `clock_timestamp()`.

Snapshot writing uses `FOR UPDATE` plus token/lease validation, then task-ID upsert. Source-index publication has its own build-lease lock and token; it does not reuse the investigation claim. These fences protect durable writes, not exactly-once remote computation. Snapshot existence alone does not establish completion, because task success commits separately.

Messages and queues are durable; publish/republish is persistent, mandatory-routed and confirmed. At-least-once duplicates are expected. Consumer alone ACKs after handler return; failed publication prevents normal ACK. Default business retries allow an initial attempt plus three retries. Pending/running claim contention is deferred without consuming execution attempts; missing tasks have a separate three-check bound. `not_before` defers processing, with retry queue TTL currently 60 seconds.

### 4.3 Error and cancellation policy

- Existing typed retryable/permanent processor errors retain their policy. The original runner recursively classifies ordinary processor `ExceptionGroup` leaves: invalid data (`ValueError`, including structured-output validation) and non-transient HTTP errors are permanent; HTTP 408/409/425/429/5xx, timeouts/network errors and unexpected processor failures use bounded retries. Any permanent member makes an ordinary processing group terminal; retryable groups retain the latest supplied `not_before`.
- Retry/failed state commits before the classified error reaches the messaging handler. GitHub's business service preserves its more specific rate-limit deadline classification.
- Lease-loss leaves bypass ordinary classification. The handler rereads authoritative status after plain or pure grouped lease loss: succeeded returns; pending/running defer; failed routes dead; missing uses bounded checks. A stale owner cannot release a replacement claim.
- Mixed lease/other groups, heartbeat DB failures and finalization commit failures propagate; recovery may wait for lease expiry and redelivery. External `CancelledError` propagates without reporting success or deliberately releasing ownership. Future Graph integration must validate these policies against actual provider failures.
- Invalid message JSON/schema and retry metadata route to dead; ACK still requires confirmed forwarding. Header/body errors are distinct from processor failures.

### 4.4 Trust, identity and version boundaries

Issue/history/comment/PR readers require metadata with `private` exactly `false` before reading content; source revision resolution reuses its metadata read for the same check. Missing/ambiguous visibility fails closed. Each content request adds a metadata request without caching visibility; enrichment counts include both. Tree/blob helpers accept backend-selected revisions/manifests. Visibility may change between requests; this is not private authorization or tenant isolation.

Retrieved text and model output are untrusted data. Tools require whitelist/schema checks and fixed backend permissions. Preserve repo/index boundaries and citation provenance; never promote tool feedback or unsupported hypotheses into verified findings. Source evidence is immutable-revision-based; History content can change. Future durable reports need the actual evidence version/text used, not only a mutable URL. Embedding-input/chunking changes require deliberate identity/version handling to prevent incompatible index reuse.

## 5. Verification Scope and Evaluation Limits

The developer confirmed the default pytest suite passed after fixing the new runner test fixture: `factory()` must yield a Session whose `__aenter__` returns itself; `session.begin()` synchronously returns an async transaction context rather than a coroutine. The focused PostgreSQL maintenance group also passed. Final aggregate counts/logs were not supplied; these are developer-reported local results, not assistant-executed tests or observed CI.

| Coverage layer | Evidence / limits |
| --- | --- |
| Unit and compiled-Graph tests | Cover routing, citation rejection, query/tool validation, novelty, nested failures, lease handling, cancellation, poison-message ACK ordering and visibility; model/search dependencies are mocked |
| Source adapter PostgreSQL regression | Real compiled Graph → adapter → existing M3 search → pgvector, with fake embeddings and isolated index cleanup; includes error/cancellation and transaction probes |
| Processor PostgreSQL regression | Real task claims/status transactions with a fake publisher; not a live RabbitMQ/LLM Agent E2E |
| Existing DB/broker integration suites | Opt-in fixtures cover ownership, outbox, snapshots and delivery; do not infer that every opt-in suite ran from a default pytest success |
| Historical live LLM tests | Task 59 Query Planner and Task 60 function calling were reported successful; Tool Calling used mocked retrieval evidence. The live analyzer file also supplies controlled fake retrieval inputs |
| Production Worker → real RAG → Graph → durable report | Not implemented or verified |

Source evaluation uses [eight bilingual questions](../tests/fixtures/retrieval/sampleproject.json) over a pinned tiny repository and only two independent targets. Historical Semantic and Automatic Hybrid Recall@5/MRR@5 were both 1.000; this does not establish Hybrid superiority or general Issue accuracy. Evaluation implementation is under `src/maintainer_lens/evaluation/`, not a root-level `evaluation/` directory.

The extended History benchmark has six labeled Issues, 34 dynamically fetched distractors and 14 queries. Historical semantic Top-1/Recall@5/MRR@5 were 92.9%/100%/0.964, versus keyword 57.1%/64.3%/0.607. Its live test prints metrics without enforcing those historical scores as fixed thresholds; corpus contents can change, relevance labels are incomplete, and it does not measure PR/comment ranking. HNSW/reranking changes require representative evidence of need.

Keep ordinary mocks independent of `.env`; real DB, broker and paid-provider tests require their explicit flags. Inspect the selected test's prerequisites and cleanup before running it. A skipped test is not a verified integration, and local green tests do not establish merge or deployment.

## 6. Extension Points

| Proposed change | Start with | Required contract before implementation |
| --- | --- | --- |
| Connect Graph to Worker | `worker.run_worker()` production callback, `services/issue_investigator.py`, existing runner; then Graph State and adapter factories | Design one processor inside the existing claim/lease boundary; decide knowledge readiness/index selection, client lifetimes, total deadline, validated terminal result and failure mapping. No second task runner/status engine |
| Persist or expose investigation reports | Investigation ORM, fenced Snapshot writer pattern, task finalization, HTTP detail schema/query | Define report/evidence identity and migration; commit fenced required report writes before success; handle duplicates and snapshot/status commit windows |
| Improve retrieval quality or provider compatibility | Existing Source/History search and Embedding protocol, then evaluation rows above | Preserve index/model/dimension/version isolation; measure recall/rank failure first; avoid concurrent calls on one Session |
| Refresh more History or add source formats | Sync/backfill/enrichment/indexing separately; format policy + syntax registry/chunker | Keep bounded requests, monotonic writes, resumable cursors and embedding refresh explicit; do not silently equate a partial batch with full coverage |
| Add a retrieval tool or follow-up strategy | Existing protocol/schema, tool-node adapter and graph composer | Backend fixes access context; validate one allowed call per step; preserve round/query budgets, novelty checks, citations and cancellation; no redundant fixed-node rerun after a tool |
| Add durable workflow recovery, HITL or memory | Future M5 design around Graph compilation/State and existing task identity | A LangGraph checkpoint stores workflow state, not task ownership or broker ACK state. Define recovery/duplicate-cost behavior before enabling it |
| Change schema, deployment or CI | ORM + migrations + API/writers/tests; Compose/lifespan + CI | Preserve resource cleanup and compatibility, verify actual migrations/check results, and keep secrets/local notes outside public artifacts |

These are navigation and design constraints, not accepted implementations. The next small engineering discussion is the Graph/processor integration contract; whole-Graph controls, durable reporting and Worker wiring remain pending design and validation. The prior proposal for a competing `services/investigation_runner.py` is not accepted.
