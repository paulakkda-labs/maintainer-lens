# MaintainerLens Architecture

MaintainerLens investigates public GitHub Issues using repository source and
historical discussions. V1 is read-only. This document describes implemented
components, their execution boundaries and the remaining integration work.
See the [Roadmap](ROADMAP.md) for milestones and [ADRs](ADR/README.md) for decisions.

## System overview

| Boundary | Responsibility | Current integration |
| --- | --- | --- |
| Reliable task backend | HTTP submission, transactional Outbox, RabbitMQ delivery, claims, leases and task status | Default Worker fetches and persists a public Issue snapshot |
| Knowledge services | Source/document indexing, History ingestion, embeddings, retrieval and evaluation | Independently callable; Graph adapters reuse these services |
| Investigation application | Validated input/result, bounded LangGraph, whole-Graph deadline, fenced result storage and business processor | Independently callable; default Worker has not switched to this processor |
| HTTP readback | Task status, available Issue snapshot and optional completed investigation result | Implemented; legacy snapshot-only tasks legitimately return a null result |

PostgreSQL is authoritative for task state and artifacts. RabbitMQ transports
at-least-once work signals. The existing root-level
[investigation runner](../src/maintainer_lens/investigation_runner.py) owns task
claims, lease renewal and status transitions. LangGraph owns investigation
nodes and edges. Redis currently has infrastructure/lifecycle integration;
it is not an implemented Agent memory or retrieval cache.

FastAPI, the Outbox Dispatcher and Worker run as separate processes.
[Compose](../compose.yaml) also provides PostgreSQL 17 with pgvector, Redis 7.4,
RabbitMQ 4.2 and a one-shot Alembic migration service. Local Compose is a
localhost-bound development setup, not a hardened public deployment.

## Module navigation map

| Area | Entry points | Responsibility |
| --- | --- | --- |
| HTTP and submission | [routes](../src/maintainer_lens/api/routes/investigations.py), [shared contracts](../src/maintainer_lens/schemas/investigation_contracts.py), [submission](../src/maintainer_lens/services/investigation_submission.py) | Validate requests; persist Task + Outbox atomically; return typed details |
| Delivery | [Dispatcher](../src/maintainer_lens/services/outbox_dispatcher.py), [handler](../src/maintainer_lens/messaging/handler.py), [consumer](../src/maintainer_lens/messaging/consumer.py) | Confirmed publication, retry/dead-letter routing and ACK ownership |
| Task lifecycle | [runner](../src/maintainer_lens/investigation_runner.py), [task DB](../src/maintainer_lens/db/investigation_tasks.py), [Worker](../src/maintainer_lens/worker.py) | Claim/lease/status, error classification and process resource assembly |
| Snapshot and Graph processor | [Issue service](../src/maintainer_lens/services/issue_investigator.py), [processor](../src/maintainer_lens/services/investigation_processor.py) | Commit snapshot data; optionally compose Graph execution and result commit under an existing claim |
| Graph | [application entry](../src/maintainer_lens/services/investigation_execution.py), [composer](../src/maintainer_lens/agents/investigation_graph.py), [node modules](../src/maintainer_lens/agents/investigation/) | Fresh State, deadline, retrieval/analysis/controlled follow-up and final validation |
| Source knowledge | [indexing](../src/maintainer_lens/services/repository_indexing.py), [search](../src/maintainer_lens/services/repository_search.py) | Commit-pinned indexes, build leases, exact semantic/optional keyword retrieval |
| History knowledge | [preparation](../src/maintainer_lens/services/repository_history_pipeline.py), [sync](../src/maintainer_lens/services/repository_history_sync.py), [semantic retrieval](../src/maintainer_lens/services/repository_history_semantic_search.py) | Bounded collection/enrichment/indexing and repository-scoped evidence retrieval |
| Result storage/readback | [ORM](../src/maintainer_lens/db/models/investigation_result.py), [writer/decoder](../src/maintainer_lens/db/investigation_results.py), [detail query](../src/maintainer_lens/db/investigation_queries.py) | Versioned JSONB artifact, claim fencing, strict decoding and one-statement detail reads |

## Execution flows

### Default HTTP-to-Worker path

```text
POST /investigations
  → PostgreSQL: InvestigationTask + InvestigationOutboxEvent, one commit
  → Dispatcher: claim → confirmed RabbitMQ publish → guarded publication mark
  → RabbitMQ pending/retry/dead-letter queues
  → Worker → existing Runner: claim commit + concurrent lease renewal
  → public GitHub Issue fetch → fenced IssueSnapshot commit
  → Runner: guarded task-success commit
  → Handler returns → Consumer ACK
```

The Worker still invokes `investigate_issue()` directly. It does not invoke the
Graph processor. GitHub reads validate public visibility and Issue identity;
network requests occur outside snapshot transactions. The service returns
immutable `IssueSnapshotData` after commit and Session cleanup.

The handler can also return after confirmed retry/dead-letter forwarding.
Consequently, an ACK can establish successful routing of an unsuccessful
attempt; task completion is determined by PostgreSQL status.

### Knowledge preparation and retrieval

Source preparation resolves public commit/tree/blob identities, downloads
permitted content, creates syntax-aware chunks and embeds them into versioned
ready indexes. Identity includes repository, commit, provider/model, dimensions
and chunker version. Python, JavaScript/TypeScript, Go, Java and Markdown have
structural chunking; supported text/config formats can use window fallback.
Build claims and fenced transactional publication protect concurrent builds.

Source retrieval uses exact pgvector cosine search plus optional escaped
`ILIKE` literal matching, fused with reciprocal rank fusion (default constant
60). Queries execute sequentially when using one Session. V1 has no HNSW,
reranker, BM25 or separate search engine.

History collection, incremental sync, resumable backfill, comment/PR enrichment
and embedding refresh are separate bounded operations. The small preparation
pipeline does not advance sync cursors. Embedding bodies are truncated to 5,000
characters; content hashes and source timestamps control reuse. Semantic
retrieval returns embedded text snapshots scoped by repository/provider/model/
dimensions. Keyword retrieval is also available. PR enrichment is metadata,
not a full diff or a verified Issue-to-fix relationship.

Knowledge preparation is explicit; the investigation entry does not build
indexes, ingest history or automatically refresh embeddings.

### Complete Graph application call

`execute_graph_investigation(request, *, history_searcher, analyzer,
timeout_seconds, ...)` uses the shared `InvestigationInput` and
`InvestigationResult` models. Input contains repository, Issue number/title/body,
response language and an optional Source index UUID. Callers cannot inject
State evidence, findings, current rounds or routing controls. Source search
requires a caller-selected index; the search-round budget is 1–3, and Query
Planner and Tool Runner cannot both be configured.

The entry validates input/options, compiles the existing Graph and constructs
a fresh State. One `asyncio.timeout` covers initial retrieval, analysis and
all follow-up steps. Final validation projects a stable result rather than
returning arbitrary internal State.

```text
Initial History → optional Source → structured analysis
  → sufficient cited findings, no gaps: draft_preliminary_report → END
  → otherwise, if budget/query/evidence permit:
      deterministic query or Query Planner → fixed retrieval nodes
      OR model-selected tool → selected retriever only
      → merge/deduplicate → analyze only when novel evidence appears
  → exhausted budget / no useful follow-up / insufficient evidence:
      needs_more_evidence → END
```

Each analysis uses at most five History and five Source excerpts, bounded to
1,600 content characters each. `analysis_evidence` saves the exact latest
completed analysis context; skipped reanalysis retains that context, and initial
empty retrieval records an empty context without a model call. Final validation checks
request identity, required fields, structured analysis, terminal values, rounds,
repository/index provenance, citation IDs/URLs and finding membership in that
saved context. Ready routing must agree with `assess_next_step()`.

A later retrieval may reorder evidence or update a History object, so final
rankings/text cannot reconstruct the earlier model input. Source URLs pin
commit/path/lines. History URLs identify actual objects/comments; the investigated
Issue and its comments are excluded as independent historical evidence. These
checks do not establish semantic support for every model statement.

Tool calling permits one validated `search_history` or `search_source` call per
step. Repository/index/credentials remain backend-controlled. The real protocol
returns the same `tool_call_id`; free-text tool feedback is not a finding.
Caller-applied tool-step controls and the complete Graph deadline are separate.
Neither is a monetary budget or a guarantee that remote cancellation stops billing.

### Business processor under an existing claim

`process_graph_investigation()` is ready for injection into the existing Runner:

```text
short transaction: validate active claim and task identity
  → close Session
  → await Issue service: public fetch + fenced snapshot commit
  → build InvestigationInput from committed snapshot data
  → await complete Graph application call
  → short transaction: fenced result upsert + commit
  → return None
Existing Runner: guarded success commit
```

It creates no clients, prepares no knowledge and adds no claim/heartbeat/status
machine or retry loop. Graph input/options are validated after snapshot
preparation; the Graph deadline excludes GitHub acquisition and result commit.
A snapshot may remain after later Graph/result failure. Default Worker resource
assembly and callback replacement remain separate integration work.

## Persistence, transactions and recovery

Use independent Sessions for concurrent operations. Share engines, session
factories and appropriately managed HTTP clients. `AsyncExitStack`/lifespan owns
cleanup, including partially completed startup. Do not retain database
transactions while awaiting GitHub, embeddings, LLMs or broker publication.
The Source Graph adapter finishes its index-ownership lookup transaction before
calling retrieval services that own their own transaction scopes.

| Operation | Transaction boundary |
| --- | --- |
| Submission | Task + Outbox commit together |
| Outbox dispatch | Claim commit → network publish → guarded finalization commit |
| Runner | Claim, heartbeat and terminal updates use independent short transactions |
| Snapshot | Caller-owned short fenced write transaction |
| Graph result | Serialize before awaits; lock task by ID, then recheck running/token/lease using a new PostgreSQL `clock_timestamp()` evaluation; compare identity; upsert; caller commits |
| HTTP details | One nonlocking SELECT with two left outer joins reads task/snapshot/result; refresh cached ORM objects; request Session cleanup releases the read transaction |

One result row per task stores schema version 1, complete JSONB output and DB
timestamps. Upsert preserves creation time and replaces the payload/version.
The [Alembic revision](../migrations/versions/c4b2a6d8e901_create_investigation_results.py)
extends `789b1caf1332`; models are registered in `db/models/__init__.py`.
A writer returning `True` means SQL executed, not that the transaction committed.

Snapshot/result commits and Runner success commit remain separate. A crash can
leave artifacts on an unfinished task. Retries may repeat remote computation;
existing artifacts are not checkpoints or computation caches. Fencing protects
writes from stale owners, not exactly-once LLM execution.

GET exposes a result only for `succeeded` tasks. Other states return null even
if an earlier attempt committed a result. Legacy succeeded tasks without a row
also return null. Decoder checks version, JSON/model structure and task identity;
unsupported/corrupt stored results on successful tasks produce a fixed safe
HTTP 500. GET does not rerun Graph provenance/routing or fact verification.
Both Graph terminal values are valid successful outcomes after required commits.
`draft_preliminary_report` is a marker, not a final report-writing node.

## Error and trust boundaries

- Only the Graph's own expired deadline becomes `InvestigationDeadlineExceeded`.
  `timeout_scope.expired()` distinguishes upstream timeouts and catches swallowed
  deadline cancellation after normal return. External cancellation propagates.
- Explicit invalid returned analysis/citations/terminal data use
  `InvalidInvestigationResultError`; dependency errors retain their identity and
  exception-group structure at the application boundary.
- The existing Runner classifies ordinary processor exception groups recursively:
  invalid data and non-transient HTTP errors are permanent; timeout/network/
  transient HTTP and unexpected failures follow bounded retry policy. Existing
  typed errors retain their policy. Permanent group members take precedence.
- Plain/pure grouped lease loss triggers authoritative status rechecks in the
  handler. A stale owner never releases a replacement claim. Mixed groups,
  heartbeat DB errors, finalization failures and cancellation remain visible.
- Invalid message bodies/retry headers require confirmed dead-letter publication
  before ACK. Readback maps only `StoredInvestigationResultError` to its fixed
  HTTP error; DB errors and cancellation propagate through Session cleanup.
- Public metadata must explicitly report `private=false` before Issue/History/
  comment/PR content reads. Text/model output is untrusted. Visibility checks
  and fixed backend tool scope do not implement private-repository authorization.

## Verification and extension points

Tests distinguish default isolated dependencies, real compiled Graphs with fake
providers, opt-in PostgreSQL/broker integration and opt-in paid live-provider
checks. See [test instructions](../README.md#integration-tests) and the focused
[application](../tests/agents/test_investigation_application_execution.py),
[persistence](../tests/integration/test_investigation_result_persistence.py),
[processor](../tests/integration/test_investigation_processor_pipeline.py) and
[readback](../tests/integration/test_investigation_result_readback.py) suites.
CI infrastructure coverage is not a live Worker→Graph→provider E2E.

Retrieval benchmarks are small: eight bilingual Source queries over two targets,
and 14 History queries with six labeled targets plus 34 distractors. They do not
establish general Issue accuracy, PR/comment ranking quality or a need for
approximate search/reranking. Evaluation code is under
[src/maintainer_lens/evaluation](../src/maintainer_lens/evaluation/).

Next integration must select an explicit knowledge/language/budget/provider
profile, assemble existing adapters/analyzer with process resource cleanup and
switch the default Worker callback while preserving Runner/handler/ACK behavior.
Source index selection/preparation needs an explicit policy. Final report
writing, Checkpointer recovery, HITL, cross-run memory, cost accounting and private
access remain future work; none replace the existing task lifecycle.
