# MaintainerLens Architecture

MaintainerLens investigates public GitHub Issues using repository source and
historical discussions. V1 is read-only. This document describes implemented
components, their execution boundaries and the remaining integration work.
See the [Roadmap](ROADMAP.md) for milestones and [ADRs](ADR/README.md) for decisions.

## System overview

| Boundary | Responsibility | Current integration |
| --- | --- | --- |
| Reliable task backend | HTTP submission, transactional Outbox, RabbitMQ delivery, claims, leases and task status | Default Worker invokes the existing Graph processor under the original Runner |
| Knowledge services | Source/document indexing, History ingestion, embeddings, retrieval and evaluation | Independently callable; Graph adapters reuse these services |
| Investigation application | Validated input/result, bounded LangGraph, whole-Graph deadline, fenced result storage and business processor | Default Worker binds prepared History, optional explicit Source and bounded model-selected tools |
| HTTP readback | Task status, available Issue snapshot, completed result and derived Markdown report | Legacy snapshot-only tasks legitimately return null result/report |

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
| Investigation Source selection | [selection boundary](../src/maintainer_lens/services/investigation_source_index.py), [adapter](../src/maintainer_lens/agents/investigation/retrieval.py) | Validate an explicit UUID against repository, ready status and full embedding identity; return immutable metadata, without building or choosing latest |
| History knowledge | [preparation](../src/maintainer_lens/services/repository_history_pipeline.py), [sync](../src/maintainer_lens/services/repository_history_sync.py), [semantic retrieval](../src/maintainer_lens/services/repository_history_semantic_search.py) | Bounded collection/enrichment/indexing and repository-scoped evidence retrieval |
| Result storage/readback | [ORM](../src/maintainer_lens/db/models/investigation_result.py), [writer/decoder](../src/maintainer_lens/db/investigation_results.py), [detail query](../src/maintainer_lens/db/investigation_queries.py) | Versioned JSONB artifact, claim fencing, strict decoding and one-statement detail reads |
| Human-readable report | [renderer](../src/maintainer_lens/services/investigation_report.py) | Pure Markdown derived from validated stored analysis and its actual citations |
| Knowledge budget | [contracts/presets](../src/maintainer_lens/schemas/investigation_budget.py), [submission](../src/maintainer_lens/services/investigation_submission.py) | Resolve profile once, persist immutable task intent snapshot, expose choices and stored limits without modifying Graph budgets |

## Execution flows

### Default HTTP-to-Worker path

```text
POST /investigations
  → PostgreSQL: InvestigationTask + InvestigationOutboxEvent, one commit
  → Dispatcher: claim → confirmed RabbitMQ publish → guarded publication mark
  → RabbitMQ pending/retry/dead-letter queues
  → Worker → existing Runner: claim commit + concurrent lease renewal
  → compare message/task repository, Issue and Source UUID
  → optional Source metadata preflight, then public GitHub fetch + fenced Snapshot commit
  → initial History + optional Source → analysis → bounded selected-tool follow-up
  → fenced result commit
  → Runner: guarded task-success commit
  → Handler returns → Consumer ACK
```

The default Worker invokes `process_graph_investigation()`, which reuses
`investigate_issue()`. GitHub reads validate public visibility and Issue identity;
network requests occur outside snapshot transactions. The service returns
immutable `IssueSnapshotData` after commit and Session cleanup.

The handler can also return after confirmed retry/dead-letter forwarding.
Consequently, an ACK can establish successful routing of an unsuccessful
attempt; task completion is determined by PostgreSQL status.

### Worker dependency assembly

`run_worker()` uses existing settings and factories for prepared History, optional
Source and structured analysis. The backend profile is Chinese, up to three
search rounds, Top-5 per retriever, provider identity `openai-compatible`, a
120-second whole-Graph deadline and a 60-second deadline per tool step. It binds
Tool Runner, keeping Query Planner as an independent mutually exclusive option.
History-only requests expose only `search_history`; explicit Source requests also
expose `search_source`. Initial retrieval searches each enabled corpus once;
subsequent steps call only the selected retriever and reanalyze only novel evidence.
Embedding model/dimensions must match the selected knowledge. No automatic
knowledge preparation, latest-index selection or fallback is performed.

Separate GitHub, embedding and LLM HTTP clients keep transport settings and
credentials apart. Their read timeouts are 15, 60 and 90 seconds respectively;
embedding/LLM connect timeouts are 10 seconds. Factories validate required
configuration before broker connection/consumption. `AsyncExitStack` registers
each acquired resource immediately and closes channels, connection, HTTP
clients and engine in reverse order on return, startup failure or cancellation.
An injected investigator skips all default provider/client assembly.

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

Investigation Source selection uses an explicit existing index UUID.
`get_investigation_source_index()` reuses repository-format validation and checks
UUID/provider/model/dimension inputs before querying. It refreshes the selected
ORM row, checks repository ownership first, requires ready status and compares
provider, model and configured dimensions. It returns a frozen scalar metadata
snapshot containing UUID, commit/tree SHA, embedding identity and chunker version.
The selected UUID pins those stored versions; selection does not require the
repository's current remote HEAD or an arbitrarily newer chunker/index.

The caller owns the read transaction. The helper does not lock, commit, build
knowledge, select a fallback or retry. `InvestigationSourceIndexNotReadyError`
extends the existing `RepositoryIndexNotReady` family and retains index ID/status
so callers can distinguish building from failed. Missing/foreign/incompatible
indexes raise ValueError; DB errors and cancellation propagate unchanged.
The snapshot is not an ownership lease or a guarantee of future availability.

`create_source_searcher()` now requires `embedding_dimensions` alongside provider
and model. Its short lookup transaction invokes this boundary before retrieval
can call embeddings; mismatches fail before the paid query. Existing retrievers
retain their checks, and an empty ready index remains a legal empty search.
The task model now has a nullable `source_index_id` UUID column, introduced by
[revision d7e9f2a4b601](../migrations/versions/d7e9f2a4b601_add_investigation_source_index_id.py)
after the result-table revision. Apply the migration before running the updated
ORM against an existing database. Existing rows retain NULL; there is no default,
backfill, foreign key or automatic clearing when an index is deleted. This field
preserves request identity independently of index retention; it does not prove
that an index is available or valid. API submission persists the selected UUID in Task + Outbox together; Dispatcher,
publisher and Worker carry it without changing it. Missing fields in older
messages mean NULL and must still match the database task. The processor checks
all three identity fields before external work. Source metadata validation then
runs in a separate short transaction before GitHub or paid retrieval. A building
index uses existing bounded retries; failed/missing/foreign/incompatible indexes
fail explicitly, with no History-only downgrade.
[Schema tests](../tests/integration/test_investigation_task_source_index_schema.py)
cover offline DDL and opt-in PostgreSQL persistence/lifecycle behavior.
Focused [selection tests](../tests/integration/test_investigation_source_index_selection.py)
cover isolated inputs/errors and opt-in PostgreSQL identity, cache refresh,
transaction ownership and empty-index behavior.

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

The default Worker injects `process_graph_investigation()` into the existing Runner:

```text
short transaction: validate active claim and repository/Issue/Source UUID identity
  → close Session; optional injected Source metadata validator in its own short scope
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
A snapshot may remain after later Graph/result failure. Production resources
belong to the Worker, while adapters and processor retain their Session scopes.

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
HTTP 500. Identity checks include the selected Source UUID at both fenced write
and decode boundaries. Both Graph terminal values are valid successful outcomes.

The pure [report renderer](../src/maintainer_lens/services/investigation_report.py)
derives `report_markdown` on GET from a succeeded result, retaining version-1
JSONB compatibility. It revalidates structure, shared citation/URL provenance and
the existing readiness rule for ready results. Findings resolve through saved
`analysis_evidence`, whose excerpts may differ from later retrieval text.
Summary, supported findings, hypotheses, open questions and execution notes stay
separate; untrusted text is escaped. Insufficient evidence remains visible.
Unfinished/legacy absent results have a null report. No provider, knowledge DB
lookup, new report table or second LLM inference is used. The terminal marker
itself still does not execute a report-writing Graph node; traceable citations
are not semantic fact verification.

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
  before ACK. Readback maps stored-result and report-validation integrity errors to a fixed
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
The [Worker wiring suite](../tests/test_worker_production_wiring.py) exercises
actual assembly, compiled Graph, provider adapters, Runner and Handler/Consumer
using MockTransport and fake DB/broker boundaries; it covers commit/ACK ordering,
provider failures, deadlines, lease loss and resource cleanup. This coverage
does not establish actual SQL fencing or live Worker→Graph→provider E2E.

The opt-in [live Worker smoke](../tests/integration/test_worker_graph_live_smoke.py)
adds real HTTP providers, PostgreSQL/pgvector and isolated RabbitMQ queues. It
explicitly seeds/indexes one genuine historical Issue, invokes the default
Worker, checks committed artifacts before ACK and verifies GET readback. Request
hooks now cap GitHub/embedding/Chat calls at 4/4/7; retries stay parked. It skips
pre-existing repository History and cleans only its own inserted rows/resources.
This fixture preparation does not add automatic ingestion to the Worker.
The maintainer reported a successful History-only live run on 2026-10-28,
within the 4/2/1 request caps and with normal model completion. This verifies
the bounded smoke scenario, not all retrieval quality or production deployment.
That original run excluded POST/Outbox, Source, multi-round follow-up and
semantic quality. The current History smoke follows the expanded three-round
profile; its new caps/results require a new run. It still excludes POST/Outbox,
Source and a separately deployed API process.

Retrieval benchmarks are small: eight bilingual Source queries over two targets,
and 14 History queries with six labeled targets plus 34 distractors. They do not
establish general Issue accuracy, PR/comment ranking quality or a need for
approximate search/reranking. Evaluation code is under
[src/maintainer_lens/evaluation](../src/maintainer_lens/evaluation/).

Source UUID propagation, bounded production Tool Runner and derived Markdown
reports are now implemented; their new regression suites await maintainer execution.
[Full-chain tests](../tests/integration/test_worker_graph_source_e2e.py) cover real
PostgreSQL/MQ with fake providers, including POST/Outbox, both retrieval profiles,
preflight rejection, pre-ACK durability and duplicate delivery. A separately gated
paid Source+History fixture prepares one verified Source chunk and one History
document itself, takes the writer-generated UUID into POST, cleans only its own
index/data, caps requests at GitHub/embedding/LLM 8/6/7 and parks retries. This
partial test index does not change production knowledge-preparation policy. GET runs through an in-process ASGI
app, not a separately deployed HTTP server. Supplying tests is not evidence they
passed. Final quality/semantic evaluation and CI/review remain required before M4
closure. Checkpointer, HITL, cross-run memory, monetary budgets and private access
remain future work; none replace the task lifecycle.

### Automatic preparation schema staged; execution not yet enabled

The task model stages `auto_prepare_knowledge` (non-null Boolean, database
default false), `prepared_source_index_id` (nullable native UUID), and
`knowledge_budget` (non-null versioned JSONB object), through
[revision e8b3c7d9a204](../migrations/versions/e8b3c7d9a204_add_investigation_knowledge_preparation.py),
which follows `d7e9f2a4b601`. The maintainer has not applied this revision, so
budget storage was added to that same migration rather than a new revision.
Existing rows/inserts receive legacy mode, no prepared index and the original
standard-v1 budget snapshot. The original `source_index_id` preserves explicit
caller choice; the prepared field is backend-owned. Neither UUID has a retention
FK. Checks reject conflicting modes and non-object budget payloads.

[Budget contracts](../src/maintainer_lens/schemas/investigation_budget.py) define
strict, frozen snapshots with schema/profile versions. Counts and seconds are
positive integers; unlimited limits are JSON null, with no mixed finite limits.
`GET /investigations/budget-profiles` lists small/standard/large/unlimited choices.
POST selects `knowledge_budget_profile` (standard by default); submission
resolves it before opening a Session and commits the complete snapshot with Task
and Outbox. The broker remains an identity signal, not a second configuration
store. Task GET validates/returns the snapshot without looking up current presets;
corrupt budgets return a fixed safe HTTP 500. A profile-version change must not
reinterpret older stored limits. Legacy SQL defaults stay frozen independently
of future application preset changes.

`InvestigationIdentity` contains only repo/Issue/explicit-index fields. HTTP
submission adds profile choice; Graph input inherits identity directly, keeping
budgets out of Graph State/result and preserving result JSONB schema version 1.
Budget choice does not alter Graph's three-round/120-second limits or transport,
content/provenance checks. The default remains 500 Source files, 8 MiB, 2,000
chunks, three History parents and 900 preparation seconds. See the
[profile table](../README.md#knowledge-preparation-budget-profiles) for all choices.

Automatic execution remains unwired. The next integration must compare stored
mode with delivery intent, consume the stored budget values, reuse compatible
ready knowledge, adapt the in-memory/History services for unlimited preparation,
and bind prepared identity under the original claim/lease fence. File/byte checks
precede Blob downloads and chunk limits precede embedding. Result checks must use
the effective selected UUID. Schema checks alone do not enforce ownership,
immutability, readiness or budget execution. Do not manually enable automatic
rows as a supported API. Paid verification stays on small repositories.
[Schema tests](../tests/integration/test_investigation_knowledge_preparation_schema.py)
and [snapshot tests](../tests/integration/test_investigation_budget_snapshot.py)
cover isolated contracts/DDL/API behavior and opt-in PostgreSQL defaults,
constraints, durability, retry preservation and rollback. Apply the revised
migration before updated ORM usage; assistant-supplied tests await execution.

### Product intake boundary still to implement

The intended user-facing flow accepts an Issue URL and manages knowledge selection/
preparation internally. Swagger currently exposes the lower-level repository/
Issue/optional Source UUID contract, followed by GET polling for the report.
Source UUIDs are generated when knowledge indexes are created; investigation task
UUIDs are generated at submission. Neither requires the other to exist first.
The current passing plumbing checks must not be described as implementing URL-only
intake, automatic knowledge preparation or a finished user interface.

The maintainer confirmed the bounded real Source+History POST/Outbox/Worker/GET
smoke passed. Its fixture prepares one real Source chunk; this does not verify
the future production automatic-preparation profile or URL-only intake.
