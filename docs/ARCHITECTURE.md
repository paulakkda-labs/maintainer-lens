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
| Investigation application | Validated input/result, bounded LangGraph, whole-Graph deadline, fenced result storage and business processor | Default Worker supports automatic Source/History preparation or explicit knowledge, then bounded model-selected tools |
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
| Graph | [application entry](../src/maintainer_lens/services/investigation_execution.py), [composer](../src/maintainer_lens/agents/investigation_graph.py), [node modules](../src/maintainer_lens/agents/investigation/) | Validated fresh input, retrieval/analysis/controlled follow-up and shared final validation |
| Durable Graph execution | [execution contract](../src/maintainer_lens/schemas/investigation_checkpoint.py), [resume service](../src/maintainer_lens/services/investigation_checkpoint_execution.py), [Saver](../src/maintainer_lens/agents/investigation/checkpointing.py), [SQL store](../src/maintainer_lens/db/investigation_checkpoints.py), [models](../src/maintainer_lens/db/models/investigation_checkpoint.py) | Frozen input/configuration, task-scoped checkpoints/pending writes and claim-fenced recovery |
| URL reuse and freshness | [resolver](../src/maintainer_lens/services/investigation_url_reuse.py), [arbitration](../src/maintainer_lens/db/investigation_reuse.py), [metadata observation](../src/maintainer_lens/services/investigation_freshness.py), [shared execution profile](../src/maintainer_lens/services/investigation_configuration.py) | Resolve active/cached/recoverable/changed URL intent without mixing versions |
| Progress and errors | [reporter](../src/maintainer_lens/services/investigation_progress.py), [event store](../src/maintainer_lens/db/investigation_progress.py), [event models](../src/maintainer_lens/db/models/investigation_reuse.py) | Claim-fenced phases, safe processing errors, delivery generation and paginated API history |
| Human review | [contracts](../src/maintainer_lens/schemas/investigation_review.py), [service](../src/maintainer_lens/services/investigation_review.py), [store](../src/maintainer_lens/db/investigation_review.py), [routes](../src/maintainer_lens/api/routes/investigation_reviews.py) | Claim-fenced pause, immutable budget decisions, idempotent Outbox resume and validated partial acceptance |
| Source knowledge | [indexing](../src/maintainer_lens/services/repository_indexing.py), [search](../src/maintainer_lens/services/repository_search.py) | Commit-pinned indexes, build leases, exact semantic/optional keyword retrieval |
| Investigation Source selection | [selection boundary](../src/maintainer_lens/services/investigation_source_index.py), [adapter](../src/maintainer_lens/agents/investigation/retrieval.py) | Validate an explicit or backend-selected UUID against repository, ready status and full embedding identity; return immutable metadata, without building or choosing latest |
| History knowledge | [preparation](../src/maintainer_lens/services/repository_history_pipeline.py), [sync](../src/maintainer_lens/services/repository_history_sync.py), [semantic retrieval](../src/maintainer_lens/services/repository_history_semantic_search.py) | Bounded collection/enrichment/indexing and repository-scoped evidence retrieval |
| Result storage/readback | [ORM](../src/maintainer_lens/db/models/investigation_result.py), [writer/decoder](../src/maintainer_lens/db/investigation_results.py), [detail query](../src/maintainer_lens/db/investigation_queries.py) | Versioned JSONB artifact, claim fencing, strict decoding and one-statement detail reads |
| Human-readable report | [renderer](../src/maintainer_lens/services/investigation_report.py) | Pure Markdown derived from validated stored analysis and its actual citations |
| Automatic intake/preparation | [URL contract](../src/maintainer_lens/schemas/investigation_url.py), [preparer](../src/maintainer_lens/services/investigation_preparation.py), [binding DB](../src/maintainer_lens/db/investigation_preparation.py), [Source spool](../src/maintainer_lens/services/repository_index_preparation.py) | URL identity, persisted limits, complete compatible index reuse/build and fenced prepared identity |
| Knowledge budget | [contracts/presets](../src/maintainer_lens/schemas/investigation_budget.py), [submission](../src/maintainer_lens/services/investigation_submission.py) | Resolve profile once, persist immutable task intent snapshot, expose choices and stored limits without modifying Graph budgets |

## Execution flows

### Default HTTP-to-Worker path

```text
POST /investigations/from-url (automatic) or /investigations (explicit knowledge)
  → PostgreSQL: InvestigationTask + InvestigationOutboxEvent, one commit
  → Dispatcher: claim → confirmed RabbitMQ publish → guarded publication mark
  → RabbitMQ pending/retry/dead-letter queues
  → Worker → existing Runner: claim commit + concurrent lease renewal
  → compare message/task repository, Issue, requested Source UUID and preparation mode
  → load frozen Graph run, if present; compare code/model/configuration
  → validate effective Source index in its own short scope
  → new run: public fetch + fenced Snapshot; automatic preparation + UUID binding
  → new run: freeze trusted InvestigationInput and execution configuration
  → fresh input or restored checkpoint → retrieval/analysis/bounded follow-up
  → sync checkpoint durability before each next super-step and Graph return
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
Legacy History-only requests expose only `search_history`; explicit or automatic Source requests also
expose `search_source`. Initial retrieval searches each enabled corpus once;
subsequent steps call only the selected retriever and reanalyze only novel evidence.
Embedding model/dimensions must match the selected knowledge. URL requests
explicitly enable automatic preparation; lower-level requests do not. No arbitrary
latest-index selection or cross-configuration fallback is performed.
The Worker explicitly passes `thinking_enabled=False` to the existing analyzer,
matching the tool protocol. Analysis defaults to 4,096 output tokens, with the
positive-integer `max_output_tokens` factory argument available to application
callers. Boolean/non-integer/non-positive values fail before dependency use;
model-specific output/context limits remain the provider's responsibility.
The default Worker uses this factory default. Prompts request clear, concise
findings and uncertainty without fixed character/finding-count caps, preserving
the full selected evidence context. Repository preparation and retrieval coverage
are independent of this per-analysis generation budget. No single output limit
establishes coverage of arbitrary repositories or semantic accuracy. These
instructions do not restrict historical result schemas or make validity
guarantees about provider output. `finish_reason=length` is rejected
before JSON parsing, including otherwise valid JSON; no new retry is introduced.

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
runs in a separate short transaction before GitHub or paid retrieval for explicit
selections; automatic selections are validated after preparation. A building
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

The standalone Graph execution entry does not prepare knowledge. URL-mode
Processor preparation happens before Graph execution, through an injected
preparer using Worker-owned clients. History sync/backfill remains independent.

### Complete Graph application call

`execute_graph_investigation(request, *, history_searcher, analyzer,
timeout_seconds, ...)` uses the shared `InvestigationInput` and
`InvestigationResult` models. Input contains repository, Issue number/title/body,
response language and an optional Source index UUID. Callers cannot inject
State evidence, findings, current rounds or routing controls. Source search
requires an application-selected index; the search-round budget is 1–3, and Query
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
short transaction: validate claim, repository/Issue/requested UUID/mode; load saved auto budget
  → close Session; load/compare frozen execution if checkpointing is enabled
  → optional Source metadata validator in its own short scope
  → no frozen run: await Issue service, preparation and fenced UUID binding
  → no frozen run: freeze input/configuration in a short fenced transaction
  → existing run: reuse its Issue input and effective index, without HTTP/preparation
  → await checkpointed Graph application (or unchanged standalone entry)
  → short transaction: fenced result upsert + commit
  → return None
Existing Runner: guarded success commit
```

It creates no clients and adds no claim/heartbeat/status machine or retry loop.
Automatic preparation uses the injected service and stored limits; a previously
bound index is validated and reused without another preparation call. Graph
input/options are validated after snapshot/preparation. Its active-segment deadline excludes
GitHub snapshot acquisition, knowledge preparation, frozen-run setup and result commit.
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
| Submission | Low-level Task + Outbox commit together; URL lookup arbitration and a new generation/event commit atomically |
| Outbox dispatch | Claim commit → network publish → guarded finalization commit |
| Runner | Claim, heartbeat and terminal updates use independent short transactions |
| Human review | Runner suspension: request/event/conditional lease release in one transaction; decision: Task lock, decision/counters/Outbox/event commit together, no provider I/O |
| Snapshot | Caller-owned short fenced write transaction |
| Graph progress | Each run/checkpoint/pending-write callback owns a short Session/transaction; lock Task, validate frozen identity and fresh claim/lease, write, then recheck ownership before commit |
| Graph result | Serialize before awaits; lock task by ID, then recheck running/token/lease using a new PostgreSQL `clock_timestamp()` evaluation; compare identity; upsert; caller commits |
| HTTP details | One nonlocking SELECT with two left outer joins reads task/snapshot/result; refresh cached ORM objects; request Session cleanup releases the read transaction |

One result row per task stores schema version 1, complete JSONB output and DB
timestamps. Upsert preserves creation time and replaces the payload/version.
The [Alembic revision](../migrations/versions/c4b2a6d8e901_create_investigation_results.py)
extends `789b1caf1332`; models are registered in `db/models/__init__.py`.
A writer returning `True` means SQL executed, not that the transaction committed.

Snapshot/result commits and Runner success commit remain separate. A crash can
leave artifacts on an unfinished task. Retries may repeat remote computation;
Snapshot/result artifacts are not checkpoints. Separate Graph checkpoints now
reuse committed progress; in-flight calls can still repeat. Fencing protects
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
That older fixture is distinct from the automatic-preparation Worker path.
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
reports were merged in PR #13 with maintainer-reported passing tests.
[Full-chain tests](../tests/integration/test_worker_graph_source_e2e.py) cover real
PostgreSQL/MQ with fake providers, including POST/Outbox, both retrieval profiles,
preflight rejection, pre-ACK durability and duplicate delivery. A separately gated
paid Source+History fixture prepares one verified Source chunk and one History
document itself, takes the writer-generated UUID into POST, cleans only its own
index/data, caps requests at GitHub/embedding/LLM 8/6/7 and parks retries. This
partial test index is excluded from automatic reuse by its unique chunker version. GET runs through an in-process ASGI
app, not a separately deployed HTTP server. Supplying tests is not evidence they
passed. M4 is merged in PR #14; broad semantic quality remains a release acceptance concern.
Checkpoint recovery is merged in PR #15 with maintainer-confirmed acceptance, as described below.
On-demand review below is merged in PR #17 with maintainer-confirmed local
acceptance. Combined workflow closure acceptance is pending; curated cross-run
memory is deferred, and monetary budgets and private access remain future work;
none replace the task lifecycle.

### Automatic URL intake and knowledge preparation

`POST /investigations/from-url` accepts only a canonical
`https://github.com/owner/repository/issues/positive-number` URL (optional final
slash) and a preparation profile. Userinfo, ports, query/fragment, encoded paths,
PR links and internal execution fields are rejected. Parsing yields the existing
repository/Issue identity; the submitted URL is never fetched directly. The
original `/investigations` endpoint retains its lower-level explicit-knowledge
contract. Neither endpoint waits for preparation or models.

The existing Task schema (`e8b3c7d9a204`, following `d7e9f2a4b601`) stores:
`auto_prepare_knowledge` (default false), backend-only
`prepared_source_index_id`, and the strict versioned `knowledge_budget` JSONB
snapshot. No new migration/table is added. Mode checks prevent simultaneous
automatic and explicit selection; UUIDs retain identity without retention FKs.
The submission resolves the profile before its Session, then commits Task and
Outbox together. Messages carry the requested identity and automatic flag, never
a second budget configuration. Older messages default to false and must match
the Task. Stored limits are consumed as-is on retries, independent of preset
changes; corrupt budget data fails before external work.

[The preparer](../src/maintainer_lens/services/investigation_preparation.py) binds
Worker-owned GitHub/embedding clients and Session factory. It runs Source then
History under one preparation `asyncio.timeout`, separate from Graph. Timeout
translation checks `expired()`; upstream timeouts, external cancellation, leases
and exception groups keep their meaning. A dependency swallowing own timeout
cancellation cannot produce success or continue into History. GitHub rate-limit
HTTP responses reuse the existing retry-deadline classification. Existing Runner
and Handler own retry/status/routing; no inner retry loop is introduced.

Automatic Source resolves a pinned public default-branch revision and reuses only
an exact ready index for repository/commit/provider/model/dimensions and stable
complete-index version `investigation-auto-v1`. It does not pick arbitrary latest
or partial smoke indexes. A ready match avoids Source download/embedding work,
even if its corpus is larger than the new task's work budget. Missing indexes
use the existing index lease/renewal/fencing protocol. Concurrent build ownership
uses existing bounded task retries.

The automatic build checks all selected manifest file counts/bytes before Blob
requests. It verifies one Blob at a time, chunks and spools JSONL to a temporary
directory, and checks total chunk count before any Source embedding. It embeds
bounded batches into a second spool. The replayable writer validates before SQL,
then inserts at most 50 ORM records per flush in one transaction and publishes
ready only after completion and another lease check. Cooperative yields during
spool validation allow cancellation and heartbeats. No external calls occur in
that transaction. Failure rolls back the index; temporary files close/remove on
success, failure or cancellation. The old direct in-memory build API remains
available for existing callers; automatic unlimited mode bypasses its 8 MiB cap.
Manifest/identity-key metadata remains proportional to corpus size; disk and
infrastructure are finite. Allowlist, 512 KiB per-file selection, Blob SHA and
existing tree traversal controls remain unchanged.

History preparation selects recent Issue/PR parents in pages of at most five,
persists parents/details with scoped Sessions, and reuses content-identical
embeddings. A finite parent budget stops at that work scope; null pages until
exhaustion. Per page, at most two parents receive one page of five comments;
indexing uses at most ten stored comments per parent. This preserves the existing
detail policy and does not claim full History synchronization or complete PR
fix evidence. Network calls occur outside SQL transactions.

After both services complete, Processor validates Source metadata, locks the Task
and checks fresh database time/token/lease before binding UUID. Binding the same
UUID is idempotent; another UUID/mode/identity is rejected. A retry with a binding
validates it and skips preparation/HEAD resolution. Before binding, a completed
Source index or committed History page may remain after failure and be reused.
These are reusable knowledge, not successful task artifacts. Claim loss can
prevent binding even after external work; remote cancellation does not guarantee
provider billing stops.

The effective result index is `prepared_source_index_id` in automatic mode and
requested `source_index_id` otherwise. The result writer locks the Task and fences
against that effective identity; unbound automatic tasks cannot write results.
GET validates the same effective identity and returns both requested/prepared
fields plus the automatic flag, saved budget, structured result and deterministic
Markdown. Results keep schema version 1; preparation budgets do not enter Graph
State or its stable input/output. Successful corrupt/mismatched artifacts produce
a safe fixed HTTP 500. Both Graph terminal outcomes remain legitimate.

Default preparation limits are 500 selected Source files, 8 MiB, 2,000 chunks,
three History parents and 900 seconds. Small/large/unlimited choices are described
by `GET /investigations/budget-profiles` and the
[profile table](../README.md#knowledge-preparation-budget-profiles). Unlimited
removes only aggregate preparation caps/deadline, not Graph's three rounds/120
seconds, tool's 60 seconds, transport or content checks. Preparation limits are
per attempt rather than a cumulative monetary accounting system.

### Automatic-path verification and M4 acceptance

[Offline regressions](../tests/test_investigation_automatic_preparation.py) cover
URL injection/shape, snapshots, actual compiled Graph composition, binding reuse,
deadlines, upstream errors/groups, cancellation, spool cleanup, pre-embedding
limits and null limits beyond the old 8 MiB cap.
[Writer regressions](../tests/services/test_repository_index_spool_writer.py)
check replay validation, SQL batch sizes and rollback before ready publication.
[PostgreSQL tests](../tests/integration/test_automatic_investigation_binding.py)
cover mode/budget authority, rollback, idempotency, stale/replaced/expired claims,
lock-wait expiry, effective result UUID and GET corruption handling.
[Full-chain tests](../tests/integration/test_worker_graph_source_e2e.py) add cold
URL submission with actual build/History services, default Worker and simulated
providers; a second task reuses the ready index and duplicate messages do not
recompute. Finite-budget failure cannot publish a partial index.

A separate `RUN_LIVE_AUTO_PREPARATION_SMOKE=1` small-repository test uses actual
GitHub, embedding and LLM, with independently enforced operation/input/request
caps. It submits no UUID and skips before touching pre-existing matching knowledge.
It requires isolated test infrastructure and cleans only test-owned data; GET is
in-process ASGI. See [commands](../README.md#automatic-url-intake-acceptance).
The maintainer confirmed preceding local regressions/infrastructure checks and
the automatic paid smoke passed on 2026-10-29. The latter investigated
sampleproject Issue #225 from URL-only submission, used three rounds and returned
`needs_more_evidence`, with GitHub/embedding/Chat requests 22/6/6, all HTTP 200
and `stop`/`tool_calls` completions. The assistant did not independently run these
tests. Git contains the M4 squash merge in PR #14; the maintainer reported the CI
fix resolved. Valid provenance is not semantic fact verification. M5 adds recovery
as a separate boundary below; human review is merged in PR #17 with
maintainer-confirmed local acceptance. Curated project memory is deferred;
the product Web UI remains M6 work.


## Durable Graph execution and recovery

The default Worker enables a custom async `BaseCheckpointSaver` through existing
SQLAlchemy/asyncpg resources. The [Alembic revision](../migrations/versions/f9a4d6e8b205_add_investigation_checkpoints.py)
adds three application-owned tables, registered under `db/models`:

- `investigation_graph_runs`: one immutable input/configuration snapshot per Task;
  it contains no additional task status, claim or lease.
- `investigation_checkpoints`: full typed framework snapshots, channel versions,
  parent checkpoint identity and JSON metadata. The composite primary key also
  supports latest-checkpoint lookup within one task/namespace.
- `investigation_checkpoint_writes`: ordered pending node outputs, with SDK
  deduplication semantics. These reference the run directly because their callback
  can arrive before the corresponding snapshot callback.

`thread_id` is the stable business Task ID. Each Saver/SQL store is bound to the
current claim token; a replacement claim reuses the same thread. Every read/write
locks the Task, checks `running`/token/lease using fresh PostgreSQL wall time after
the lock wait, compares repository/Issue/effective index with the frozen run and
checks ownership again before commit. Concurrent callbacks own separate Sessions.
No DB transaction spans provider requests. Completed tasks cannot be modified by
an old Saver; deleting a Task cascades through all three tables.

The run freezes the Issue title/body, language, effective Source UUID, code and
SDK versions, strategy, rounds, deadlines, retrieval sizes, embedding identity,
LLM identity/output allowance and thinking mode. Endpoint identities are hashes;
credentials are not persisted. API key rotation alone does not change identity.
A configuration/version mismatch or corrupt saved identity fails explicitly,
without clearing checkpoints, selecting a new HEAD or silently restarting.
Graph semantics/prompt changes must increment the application Graph version.

`execute_checkpointed_investigation()` shares M4 input/options and terminal
validation. It validates saved identity/evidence/rounds, including pending writes,
then calls the real compiled Graph with fresh input only if no checkpoint exists;
otherwise it passes `None`. It uses `durability="sync"` before the next super-step
and completion. The serializer disables pickle and arbitrary imported types.
The Saver validates the verified checkpoint payload formats v2 (public helpers)
and v4 (the installed Graph runtime), preserving the version without rewriting
it. The checkpoint package's helper `LATEST_VERSION` is not the runtime format
version. Unknown/non-integer formats still fail; frozen application/storage and
SDK package compatibility checks remain separate.
The adapter supports async core Saver operations for the current ordinary state
channels; it does not implement subgraph/DeltaChannel reconstruction, synchronous
execution or administrative cross-task pruning/copy APIs.

One active execution segment retains the 120-second deadline. Restored rounds
keep the original 1–3 budget; existing MQ attempts remain bounded. The deadline
includes checkpoint I/O. LangGraph waits for background checkpoint callbacks on
exit, so the application shields its child Graph task only to intercept outer
cancellation, cancel Saver I/O and the child, and await cleanup before rethrowing.
This is resource cleanup, not a second task lifecycle or retry loop. Own deadline
uses `timeout_scope.expired()`; upstream errors/groups and external cancellation
retain their meanings. Cleanup may take additional time and cancellation does
not guarantee that a remote provider stops computing.

The final Graph checkpoint precedes result commit; result commit still precedes
Runner success and Consumer ACK. A crash between those commits resumes completed
Graph state, revalidates and fenced-upserts the result, then lets the existing
Runner finish. Already committed nodes are reused; an in-flight provider call
whose output was not persisted may repeat. Knowledge preparation before the run
is created is not precisely checkpointed, although ready knowledge can be reused.
There is no exactly-once or cumulative cross-attempt monetary-budget guarantee.

The offline [protocol/execution tests](../tests/test_investigation_checkpoint_execution.py)
and [contract tests](../tests/test_investigation_checkpoint_contracts.py) use real
compiled Graphs with isolated storage/provider dependencies. The opt-in
[PostgreSQL tests](../tests/integration/test_investigation_checkpoint_persistence.py)
cover persistence, lock waits, ownership, cancellation, reclaim and the
result/status commit gap. The existing
[Worker E2E suite](../tests/integration/test_worker_graph_source_e2e.py) additionally
cancels analysis and restarts the default Worker through real MQ redelivery.
The maintainer confirmed the checkpoint acceptance suites passed; Git contains PR #15.
The maintainer reports URL reuse/progress local tests passed and application services started; Git contains URL reuse squash merge PR #16. The assistant did not execute the acceptance suites.

Human review is an on-demand extension described below, with maintainer-confirmed local acceptance and PR/CI pending. Normal reports do not
require approval. Adjustable budget exhaustion may later preserve trusted
progress for a human decision; it cannot authorize invalid JSON, citations,
repository/index/claim boundaries or provider hard limits. Checkpoint storage itself introduces no review endpoints or waiting states; the extension below supplies those. Trusted long-term memory remains future work.


## Repeated URL resolution, freshness and progress

The automatic `/investigations/from-url` entry resolves existing intent instead
of always creating a Task. The low-level `/investigations` entry retains its
create-new contract. URL identity is case-insensitive repository + Issue + the
resolved preparation-budget snapshot. No caller can inject Task IDs, generations,
checkpoint State or a backend-selected Source UUID through this URL contract.

`investigation_url_lookups` holds the current Task and a separate successful-report
anchor. Insert-on-conflict and a row lock arbitrate concurrent first submissions,
checks and recoveries. Compatible existing automatic tasks can be adopted when
the lookup table is first introduced. A pending/running Task is returned without
a duplicate dispatch. A failed compatible Task can be recovered only when its
recorded failure is retryable, at most three user recoveries per Task. Legacy
failures without a recorded retryable classification are not silently restarted.

A terminal candidate acquires a 30-second lookup check token/lease in a short
transaction. GitHub HTTP runs outside SQL, within a 15-second metadata deadline;
concurrent requests share the existing check and can receive the historical
report with `checking`. A second short transaction checks token, expiry, current
Task and generation before applying the observation. Expired/replaced observations
cannot create work or overwrite a newer decision. External cancellation releases
the token where possible; lease expiry is the fallback if SQL is unavailable.
This is a metadata-check deadline, not a total HTTP-request latency guarantee.

The explicit freshness scope is Issue title/body/state/updated_at, default-branch
commit and recorded execution configuration. Frozen inputs/configuration remain
unchanged. Changes create a new Task and retain the prior report; checks which
cannot complete expose `unknown` and a safe reason, never `current`. This scope
does not promise detection of every linked/unlinked PR or discussion update.
GitHub open/closed status takes display priority and is included in Markdown;
closure alone is not described as independently verified code repair.

A successful unchanged report is returned directly. Historical and unverified
reports identify their owning Task and include the freshness warning in Markdown,
not only in JSON. The successful-report anchor remains usable while a later Task
is running or failed. GET exposes its artifact separately from the current Task's
`result`/`report_markdown`; an unfinished Task never gains a successful current
result merely because a historical report exists. There is no LLM call during
freshness inspection. HTTP clients are scoped through the existing GitHub factory
inside the probe; no new API lifespan resource or dependency is required.

`execution_generation=0` is the original delivery cycle; a user recovery atomically
changes failed → pending, increments the generation, writes a queued event and
creates its Outbox event. Outbox uniqueness is `(task_id, event_type, generation)`.
Nonzero generations travel in MQ bodies and survive retry republishing. Legacy
bodies default to zero and keep their original serialization. Claim SQL checks
the expected generation. A stale delivery cannot claim a newer generation and
is acknowledged without processing; a future generation is rejected. Checkpoint
threads still use the stable Task ID, and existing per-cycle MQ retry limits and
per-claim Graph deadlines remain unchanged.

`investigation_task_events` records queued/started/node/preparation/finalizing and
completion/retry/failure events. Each processing event uses the active claim,
short Task lock, fresh DB clock and independent Session. Lifecycle events commit
with the Runner's status transition. No provider request spans these transactions.
Node observation changes no Graph state or edges. The original Runner remains
the Worker claim/lease and automatic lifecycle owner. A model output limit is a safe typed ValueError
subclass for error reporting; it still fails result validation; the review extension can preserve the failure for an operator decision.
Raw exception strings, HTTP bodies, keys and model content are not API error text.

GET details preserves the original one-statement Task/Snapshot/Result read.
Additional progress reads are restricted to that observed generation/status so
a later recovery does not attach a future generation's phase to an old Task
snapshot. `last_error` may be historical even after success. Events have a bounded
`after`/`limit` endpoint; callers retain the last returned ID when polling. A killed
process may leave only its last committed phase until lease/redelivery recovery;
no reason is fabricated for an event which could not be persisted.

Migration `0ab5e7f9c306` extends `f9a4d6e8b205`, adds two registered ORM models,
Task generation/recoverability/freshness fields and Outbox generation. Stop old
processes, manually migrate, and restart matching API/Dispatcher/Worker code;
older Workers do not understand nonzero-generation messages. Downgrade refuses
when recovery generations exist rather than discard their delivery semantics.
The URL slice alone added no review state. Review is the separate extension
below; notification transport and the product frontend remain future work.
Curated cross-investigation memory is deferred.

Acceptance uses offline resolution/Graph/API/error contracts, real PostgreSQL
concurrency/expiry/rollback/generation tests and actual API→Outbox→MQ→default
Worker recovery with simulated HTTP. The maintainer confirmed local acceptance and Git contains PR #16. See
[README commands](../README.md#url-reuse-and-progress-acceptance).


## On-demand human decisions

The default Worker classifies only typed preparation budgets/deadlines and
analysis generation limits for review. A trusted `ReviewContext` captures the
base execution, effective budgets and, for analysis, Run fingerprint/checkpoint.
It reads only committed complete prior analysis and applies the existing scope,
result and citation validators. Pending/failed model writes are not reports.
The original Graph topology and `durability="sync"` execution are unchanged.

The existing Runner persists the request and `review_requested` event with a
fresh conditional claim/lease check, then moves running → waiting_review and
clears claim/lease in the same transaction. It returns only after commit, so ACK
is safe. No Worker waits for a person. Duplicate deliveries return without work;
a repeated URL returns awaiting_review. No automatic expiry job is introduced.

Review commands serialize on the same Task row, check review/generation/base
configuration/checkpoint identities, and make a request-scoped idempotent
decision. Approval persists the decision, queues pending under a new generation
and inserts Outbox/event together. Rejection is a terminal failed decision with
no new work. These are checked transitions within the existing lifecycle, not a
second runner or heartbeat/status subsystem.

`execution_generation` fences deliveries only. `user_recovery_count` and
`budget_resume_count` each allow three resumes. Approved preparation profiles
cannot lower other effective limits; output approvals require a larger finite
integer and respect an optional deployment ceiling. Original Task budget and
Run execution remain unchanged. Approved review records are separate effective
budget versions; Worker binds the corresponding analyzer to existing clients.
Model/embedding/endpoint/code identities are still frozen, and API keys are not
recorded. Selection remains deployment-level through existing settings.

Accepting a cached complete partial analysis also queues normal Worker work.
The Processor validates its frozen identity/result and commits the artifact,
then the Runner marks success. The report explicitly records human acceptance
of unfinished investigation progress. The Graph checkpoint may stay unfinished;
a crash before success reuses the approved result without calling the model.
Preparation without a Run can restart uncommitted work and reuse ready knowledge.
Invalid JSON/citations, provider hard limits, lease loss, Graph/tool timeouts and
mixed failures do not become approvable output. `length` may describe context as
well as output exhaustion, so raising output is not a guaranteed remedy.

Registered model `InvestigationReview`, Alembic `1bc6f8a0d417`, typed API
`/{task_id}/reviews` and `/{task_id}/reviews/{review_id}/decision` provide the
persistent boundary. The migration backfills user recoveries from old delivery
generations and refuses destructive downgrade. Supplied offline/PG/MQ tests
passed per maintainer feedback, with application services restarted. Git contains
squash merge PR #17. The assistant did not run those suites or independently
inspect CI. See [manual commands](../README.md#on-demand-human-review).

## Combined workflow acceptance and reuse boundaries

M5 closure validates composition of the existing mechanisms rather than adding
another persistence layer. The new
[workflow acceptance suite](../tests/integration/test_investigation_workflow_acceptance.py)
uses real PostgreSQL transactions, HTTP routes, the original Runner/Processor
and compiled checkpointed Graph with deterministic follow-up. GitHub, analysis,
knowledge preparation and retrieval are simulated. A test-owned ready index
supplies identity metadata; it is not an actual source-index build. Default
Worker/tool selection and broker delivery remain covered by the existing
[Source E2E suite](../tests/integration/test_worker_graph_source_e2e.py).

| Combined boundary | Acceptance expectation |
| --- | --- |
| Approved budget → transient failure → URL recovery | Latest approval remains applicable to the later generation; frozen Run and Task intent stay unchanged; user recovery and budget counters each advance only for their own action |
| Delayed delivery after either kind of resume | Old generation cannot acquire the current claim or call dependencies |
| Accepted analysis → result commit → interruption before success | Result presence is not completion; both detail and URL readback hide it while running; after lease expiry the same approval can be redelivered without another model/retrieval/preparation call |
| Earlier completed analysis versus later retrieval | Accepted findings retain the last actual analysis context; later retrieved evidence is returned without being represented as analyzed |
| New revision → new Task → waiting/rejection | Earlier successful report remains separately readable as historical; the new Task exposes its own waiting/rejection reason and no successful result |
| Replayed human command | The original decision/target remains immutable; conflicting commands cannot dispatch work or consume quota |

The tests emulate process interruption with external cancellation and advance
only their own Task lease to simulate expiry. They do not kill a deployed
process or prove exactly-once network calls. Queued Outbox generations are
inspected in SQL; the separate MQ suite proves delivery/ACK assembly. New tests
are supplied, not yet executed or maintainer-confirmed. M5 remains in closure
acceptance until manual checks and the reviewed PR/CI/merge workflow finish.
Commands are in [README](../README.md#workflow-closure-acceptance).

Persistence has distinct reuse scopes:

| Storage | Scope and purpose |
| --- | --- |
| Source RAG | Repository/commit/embedding identity; raw source and documentation evidence |
| History RAG | Repository/GitHub object identity; Issues, PRs and comments |
| Frozen Run/checkpoints | One Task; immutable execution identity and resumable Graph progress |
| Investigation result/URL lookup | Complete cited analysis and report; same-Issue reuse with scoped metadata freshness |
| Curated project memory | Not implemented; optional derived conclusions reused across different Issues, requiring applicability and revalidation |

The accepted V1 interaction remains Issue URL → asynchronous investigation →
progress/review/report readback. Neither conversational follow-up nor curated
project memory is an M5 closure requirement. Existing report acceptance is not
fact certification and does not create project knowledge. Representative M6
report evaluation should identify a concrete reuse gap before a new memory
subsystem is considered; citation validity alone still does not establish
semantic correctness.
