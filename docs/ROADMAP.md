# MaintainerLens Roadmap

> Updated 2026-10-29. M4 is merged through PR #14. M5 features and combined closure are merged through PR #18 with maintainer-confirmed tests and service restart; local Git history is verified, without an independent CI query. M6 offline checks and one cached deployment case passed per maintainer feedback. Other cases, final human semantic review and the current PR/CI remain pending. Project memory remains deferred.

## Product vision

MaintainerLens is an evidence-driven, server-side investigation platform for maintainers of public GitHub repositories. It joins source/documentation and Issue/PR history knowledge with a reliable asynchronous backend, LangGraph reasoning, validated citations, recoverable execution and on-demand human oversight. V1 focuses on Issue URL submission and report readback. Cross-investigation curated memory is optional future work. V1 is read-only on **public** repositories; private authorization, multi-tenant isolation, and authorized write actions belong to V2. It is not a coding-editor replacement or automated patch author.

## Milestone status

| Milestone | Goal | Status |
| --- | --- | --- |
| M0 | Bootstrap, project conventions, FastAPI liveness | Completed/merged (historical) |
| M1 | PostgreSQL, Redis, RabbitMQ, reliable Worker, CI | Completed/merged (historical) |
| M2 | Public Issue snapshot HTTP→outbox→RabbitMQ→Worker E2E | Completed/merged, PR #7 (historical) |
| M3 | Repository source/documentation and Issue/PR knowledge, RAG, evaluation | Completed and merged (#8–#10); evaluation limits below |
| M4 | Evidence-grounded LangGraph investigation, tool calling, reports, Worker integration | Completed/merged through PR #14; maintainer-confirmed regression/infrastructure/paid acceptance; broader report-quality evaluation remains M6 |
| M5 | Workflow checkpoints, state recovery, URL reuse and on-demand human review | Completed/merged through #18; maintainer-confirmed combined acceptance and service restart |
| M6 | V1 release readiness, evaluation, security, Web UI | In progress — focused report regressions and selected deployment cases passed; same-branch troubleshooting acceptance and final semantic assessment pending |
| V2 | Authenticated private repository and approved GitHub write operations | Future |

**Definition of done:** Implementation + meaningful tests + any required real external verification + project docs + reviewed PR/green CI + squash merge. An isolated unit test success is not proof that the feature is running in the production Worker.

## M0 — Project bootstrap — Complete

- [x] Python 3.12+ with `uv` and `src/` package layout.
- [x] FastAPI and `GET /health/live`.
- [x] Ruff, pytest/AnyIO, Apache-2.0.
- [x] Feature branch → pull request → squash-merge convention and branch protection.
- [x] Initial English README and Roadmap.

## M1 — Reliable asynchronous backend — Complete

### Infrastructure

- [x] PostgreSQL 17 + pgvector, Redis 7.4, RabbitMQ 4.2; Compose health checks, localhost-bound development ports, persistent volumes.
- [x] SQLAlchemy async engine, session factory, transaction boundaries; Alembic migrations; startup/cleanup tests.
- [x] Async Redis lifecycle and basic client integration.

### Messaging, task ownership, execution

- [x] Durable direct exchange and pending/retry/dead-letter queues.
- [x] Persistent messages, publisher confirms, mandatory routing and ACK after handler success.
- [x] Bounded retries and `not_before` deadlines, dead-letter handling.
- [x] PostgreSQL InvestigationTask state machine: `pending`, `running`, `succeeded`, `failed`.
- [x] Atomic task claims with claim tokens, 15-minute lease, periodic renewal, fenced result/terminal updates.
- [x] Independent Worker channels for consumption and confirmed publishing; QoS prefetch.
- [x] Integration coverage for duplicate claims, retries, leases, stale-worker fencing and broker failure behavior.

### CI

- [x] GitHub Actions with lint/format/default tests and a separate disposable DB+broker integration job.
- [x] Docker/Alembic/startup verification and package build.
- [x] Two CI jobs covering quality/package checks and disposable database/broker/startup integration. Paid provider tests remain opt-in.

## M2 — Public GitHub Issue intake and delivery — Complete

- [x] Typed GitHub REST client for public Issues, distinguishing PR objects and classifying rate limits, transient HTTP and network failures.
- [x] Transactional creation of InvestigationTask plus Outbox Event with HTTP `202 Accepted`.
- [x] Independent outbox dispatcher claims events, publishes with RabbitMQ confirms, marks success only after broker confirmation, and safely releases failed deliveries for retry.
- [x] Worker uses existing `src/maintainer_lens/investigation_runner.py` to claim/renew tasks and invoke `services/issue_investigator.investigate_issue()`.
- [x] Guarded IssueSnapshot upsert under matching claim token + valid lease.
- [x] HTTP `GET /investigations/{task_id}` returns status and optional snapshot.
- [x] Local host-mode real GitHub E2E (`encode/httpx#3712`) and Docker end-to-end (`pydantic/pydantic#13781`) success paths.
- [x] Historical PR #7 merged using squash strategy.

**Historical boundary:** M2 delivered snapshot-only processing. The current M4 Worker explicitly invokes the Graph processor with automatic or explicit knowledge and bounded follow-up; legacy succeeded tasks are not backfilled.

## M3 — Repository knowledge and retrieval — Complete per milestone tests

### Repository source / documentation indexing

- [x] Resolve immutable public Git commit/tree/blob identities and safely download permitted repository content.
- [x] Syntax-aware chunking: Python, JavaScript/TypeScript, Go, Java and Markdown.
- [x] Versioned repository indexes by repo, revision, embedding provider/model/dimensions and chunker version.
- [x] Async embedding client with provider replacement; historically validated 1024-dimensional Qwen-compatible text embeddings.
- [x] Ready-index reuse, PostgreSQL build leases, renewal and writer fencing against stale rebuilds.
- [x] Source-evidence provenance: repository, commit/blob, path, symbol, chunk and line span.

### Source retrieval and evaluation

- [x] Exact pgvector cosine semantic Top-K search, with validation and index scoping.
- [x] Escaped PostgreSQL `ILIKE` identifier/keyword matching; this is literal matching, not BM25.
- [x] Automatic source hybrid retrieval (semantic + optional keyword) using reciprocal rank fusion (RRF).
- [x] Dataset anchors resolving real path/line evidence to indexed chunks; Recall@K and MRR@K metrics.
- [x] Small pinned `pypa/sampleproject` live benchmark: eight bilingual queries targeting two different source targets; both retrieval modes achieved Recall@5 and MRR@5 of 1.000. **Insufficient evidence to claim hybrid general superiority.**

### GitHub Issue/PR history retrieval and evaluation

- [x] Bounded collection and incremental synchronization of Issues, PRs, comments and appropriate identities/URLs.
- [x] Persist and embed history texts, support semantic retrieval; keep keyword search for exact terms.
- [x] Controlled evaluation with 40 `encode/httpx` Issues, 14 bilingual queries: history semantic Top-1 92.9%, Recall@5 100%, MRR@5 0.964, compared with keyword Top-1 57.1%, Recall@5 64.3%, MRR@5 0.607.
- [x] Confirm M3 backend services are independently callable and reproducibly testable without Agent orchestration.

**V1 accepted choices:** Exact pgvector, no HNSW; no reranker unless representative evaluation demonstrates ranking limitations; History semantic by default, with no premature extra history hybrid engine. Evaluation remains small and incompletely labeled.

## M4 — Evidence-grounded LangGraph investigation — **In progress**

### Implemented and merged

- [x] Separate modular Graph composition, typed State and reusable History/Source adapters.
- [x] Structured cited analysis, bounded 1–3-round retrieval, query/evidence deduplication and early stopping.
- [x] Deterministic follow-up, LLM Query Planner or validated model-selected search tools; Planner and Tool Runner are mutually exclusive.
- [x] Actual function-calling protocol with whitelisted tools, fixed backend repository/index scope and matching `tool_call_id` feedback.
- [x] Explicit tool-step deadlines and metadata-only observability.
- [x] Stable `InvestigationInput`/`InvestigationResult` contracts and whole-Graph execution deadline with upstream timeout distinction and external cancellation propagation.
- [x] Save the exact latest completed analysis context; validate terminal identity, provenance, citations, rounds and ready routing.
- [x] Versioned JSONB result model, Alembic migration and lease-fenced upsert under existing task ownership.
- [x] Injected business processor: claimed identity → committed Snapshot → Graph → committed result; reuse the original Runner for lifecycle/retries.
- [x] HTTP completed-result readback, legacy null compatibility and explicit corrupt/unsupported artifact errors.
- [x] Regression suites for transactions, exception groups, lease-loss routing, malformed deliveries, public visibility and evidence boundaries.
- [x] Application/storage/processor/readback changes merged in PR #11 with maintainer-reported passing tests and CI.

Live Query Planner and tool-protocol checks used DeepSeek; the tool-protocol
example used mocked retrieval evidence. Compiled-Graph and processor tests
also use controlled provider responses. These do not establish a live default
Worker→Graph→real retrieval/provider E2E.

### Initial Worker wiring (maintainer-confirmed before the expanded profile)

- [x] Bind existing configured History retrieval/analyzer: Chinese, one round,
  Top-5, 120-second Graph deadline and prepared `openai-compatible` History.
- [x] Manage separate HTTP clients with AsyncExitStack, validate required provider
  settings before broker consumption and preserve injected investigator overrides.
- [x] Switch the default callback to the existing processor and forward existing
  LLM/embedding settings through Compose; no new settings or migrations.
- [x] Maintainer-confirmed wiring/startup and related regression tests passed.
- [x] Supply an explicitly gated live smoke with one historical document,
  real providers/DB/broker, request caps, pre-ACK durability checks and GET readback.
- [x] Maintainer-confirmed live History-only smoke passed with normal model
  completion and GitHub/embedding/LLM requests within the 4/2/1 caps.

### Source metadata boundary and task schema preparation

- [x] Select an existing index by explicit UUID; validate repository, ready status
  and embedding provider/model/dimensions, without latest/HEAD selection or builds.
- [x] Return immutable selected revision/chunker metadata under caller-owned
  transaction scopes; retain building/failed status in a typed existing error family.
- [x] Reuse the boundary in the Source adapter, including dimensions before query
  embeddings; provide complete isolated and opt-in PostgreSQL regression tests.
- [x] Maintainer-confirmed Source metadata/adapter tests passed.
- [x] Prepare nullable task `source_index_id` UUID model, additive Alembic revision
  and offline/opt-in PostgreSQL schema tests. No new table or index-retention FK.
- [x] Maintainer reported all task-schema tests passed; the assistant did not query the DB revision.

### Worker/Source/tools/reports and budget contracts — merged

- [x] Carry explicit UUID through API, atomic Task/Outbox, message and Worker;
  validate repository/Issue/index at preflight, fenced result write and decode.
- [x] Enable bounded Tool Runner: at most three rounds, 120-second Graph,
  60-second tool step, duplicate/non-novel early stopping and expiry checks.
- [x] Derive bilingual Markdown from saved validated analysis and actual context,
  preserving insufficiency without extra inference.
- [x] Add task preparation mode/backend-selected UUID and strict persisted
  small/standard/large/unlimited budgets. Existing Alembic head: e8b3c7d9a204.
- [x] Maintainer-confirmed regressions and bounded real Source+History
  POST/Outbox/Worker/GET smoke passed. Git contains squash merge PR #13.

The earlier paid fixture prepared one verified Source chunk and one History
record. It proves that explicit-index full-chain path, not automatic cold start
or general semantic accuracy. The assistant did not independently run it or
query the current database revision/CI.

### URL intake and automatic preparation — maintainer-confirmed tests passed

- [x] Strict `POST /investigations/from-url`: accept a canonical public GitHub
  Issue URL and optional preparation profile, without requiring an index UUID.
- [x] Carry automatic intent through Task/Outbox/message and compare DB mode;
  read saved budget values before external work rather than resolving presets.
- [x] Reuse exact complete compatible Source indexes, or build a pinned index
  under existing Source leases. Check files/bytes before Blob requests and all
  chunks before Source embeddings; never silently truncate to a Source budget.
- [x] Spool source/vector content and batch SQL inserts; unlimited removes
  aggregate preparation limits without removing transport/content/Graph controls.
- [x] Prepare recent History parents in pages, reusing unchanged embeddings;
  preserve the existing bounded per-page detail policy without claiming full sync.
- [x] Apply preparation deadline separately from Graph; propagate cancellation,
  distinguish upstream timeout and reject swallowed own-deadline cancellation.
- [x] Bind backend-selected UUID once under claim/lease fencing; revalidate and
  reuse an existing binding on retry. Compare effective UUID at result write/GET.
- [x] Supply complete offline, real PostgreSQL/MQ cold-start/reuse and independently
  gated small paid automatic smoke tests; update public architecture/operations/ADR.
- [x] Maintainer reports preceding local regressions and real-infrastructure checks passed.
- [x] Maintainer-confirmed automatic cold-start paid smoke passed: sampleproject
  #225, three rounds, needs_more_evidence, GitHub/embedding/Chat 22/6/6, HTTP 200
  and normal stop/tool_calls completions. The assistant did not execute the tests.
- [ ] Inspect actual report statements against returned excerpts and immutable
  Source links; record limitations without treating valid citations as truth.
- [x] Git contains squash merge PR #14; maintainer reported CI fix resolved and synchronized main before M5.

Default preparation is 500 selected Source files, 8 MiB, 2,000 chunks, three
History parents and 900 seconds. Unlimited applies only to preparation task
limits; Graph remains three rounds/120 seconds. Web UI remains M6. No new task
runner/status machine, Checkpointer, HITL, cross-run memory or monetary system
is added here.

### M4 closure and M5 entry

M4 backend integration is merged through PR #14. Paid/infrastructure acceptance
was confirmed by the maintainer, not independently executed by the assistant.
Focused report review remains useful and representative semantic quality is a
release acceptance concern; citation validity is not proof of correctness.

## M5 — Durable workflow and human review — In progress

### Rounds 1–3: checkpoint contract, storage and Worker recovery

- [x] Implement stable Task-linked thread identity, immutable Issue/configuration,
  code/SDK compatibility checks and three ORM tables with one Alembic migration.
- [x] Implement async public Saver protocol over existing SQLAlchemy/asyncpg,
  full typed snapshots/pending writes and fresh claim/lease fencing per operation.
- [x] Wire sync durability and recovery into the default Worker under the existing
  Runner; preserve saved rounds, segment deadline and existing delivery budgets.
- [x] Supply complete offline, opt-in PostgreSQL and real MQ crash/redelivery tests;
  document in-flight duplicate-call windows and result/status commit ordering.
- [x] Maintainer applied the checkpoint migration and confirmed acceptance passed.
- [x] Git contains checkpoint squash merge PR #15; deployment is not implied.

### URL product workflow bridge — merged in PR #16, local acceptance confirmed

- [x] Resolve repeated URL intent into active/cached/recovered/new investigation.
- [x] Coordinate concurrent checks and submissions without HTTP inside SQL locks.
- [x] Preserve a successful report independently of the current Task.
- [x] Scope freshness to Issue metadata, default-branch commit and configuration;
  show GitHub state first while retaining technical evidence and limitations.
- [x] Add bounded user recovery generations, stale-delivery fencing and transactional
  Outbox publication under the original Runner.
- [x] Persist claim-fenced phases and safe errors with bounded API history.
- [x] Maintainer reports local tests passed and application services started after the migration/testing workflow. The assistant did not execute these checks.
- [x] Git contains URL reuse squash merge PR #16. No independent CI observation is claimed here.

### Rounds 4–5: on-demand human decisions — merged in PR #17

- [x] Rounds 4–5 implementation: persist typed review requests and immutable
  decisions; release claim/lease while waiting; resume through transactional Outbox.
- [x] Supply request-scoped idempotency, independent recovery quotas, complete
  partial-analysis acceptance and deployment-configured output allowances.
- [x] Normal reports remain automatic. Only preparation budgets/deadlines and
  analysis generation limits trigger review. Invalid JSON/citations, ownership,
  Graph/tool deadlines and provider hard limits retain their boundaries.
- [x] Maintainer confirmed local tests passed and application services restarted after the migration/acceptance workflow. The assistant did not execute these checks.
- [x] Git contains human-review squash merge PR #17; independent CI inspection is not claimed.

### Final round: combined workflow acceptance — merged in PR #18

- [x] Supply opt-in real-PostgreSQL/API/compiled-Graph tests for approval followed
  by failed-task recovery, accepted-result commit/status interruption, and
  historical report retention when a newer investigation is rejected.
- [x] Preserve the original Runner and existing schema; no extra migration,
  status machine, project-memory pipeline or chat endpoint is introduced.
- [x] Document test layers, commands, remaining report-quality limits and the
  accepted deferral of curated project memory.
- [x] Maintainer confirmed default regression, combined PostgreSQL acceptance
  and the existing real RabbitMQ/default Worker suite with simulated providers.
- [x] Git contains closure squash merge PR #18; CI was not independently queried by the assistant.

Chat Completions still receives explicit bounded context. Persisting Graph state
is not automatic LLM memory, a new Task status machine or exactly-once messaging.

### Deferred: curated project memory and conversational follow-up

Source/History RAG already retain upstream materials, checkpoints retain one
Task's execution, and stored investigation results support report reuse.
Curated cross-Issue conclusions could reduce repeated investigation, but that
incremental benefit has not been demonstrated. Do not build another persistence
subsystem merely to meet the former milestone list. Reconsider it after M6
evaluation identifies a concrete gap that retrieval or existing-result reuse
cannot address. Any future design needs evidence, revision applicability,
revalidation and revocation; human acceptance of a partial report is not factual
certification or automatic admission to memory. Conversational follow-up is also
deferred; it is not required for the URL-to-report V1 workflow.

## M6 — V1 release readiness — In progress

### First slice: report review and deployed HTTP acceptance

- [x] Supply three curated public-Issue review prompts from one small repository,
  with explicit unsupported-claim checks and no fixed expected model answer.
- [x] Supply pure saved-context observations using existing provenance/report
  validators, with offline regression tests and no semantic accuracy score.
- [x] Supply opt-in HTTP-only local deployment acceptance with services running,
  default single-case small preparation and local review artifacts; no SQL/MQ
  manipulation, automatic approval or cleanup of persistent tasks.
- [x] Document language/file support levels and unsupported suffixes in README
  and a public support matrix; language registration is not quality validation.
- [x] Maintainer confirmed offline evaluation/protocol checks and the default
  single cached deployment case passed; this is not a cold-run claim.
- [x] Execute the other two selected deployment cases: CLI passed, closed-Issue
  analysis failed on a model finding with an empty citation array.
- [ ] Human reviewers assess each selected report's claims, relevance, versions,
  uncertainty and next action; record provider costs separately from HTTP latency.
- [x] Record initial assistant-assisted review gaps: an unsupported execution
  inference and a relevant classifier omitted from the actual analysis excerpt.
- [x] Implement a focused v2 context/prompt correction and supply real-Graph,
  provider-payload and version-change regressions. Preserve History/Graph/output
  budgets, existing storage formats and old reports.
- [x] Maintainer confirmed v2 correction tests passed; no new counts or exact
  suite scope were provided, so broader infrastructure acceptance is not inferred.
- [x] Maintainer updated the deployment and passed the selected new-investigation
  v2 case with existing Source knowledge; saved-context review confirmed both
  targeted improvements. No empty-database cold-run or broad accuracy claim.
- [x] Diagnose the closed-Issue contract failure from saved checkpoint writes
  and supply v3 schema/empty-findings guidance with regressions. Preserve strict
  finding references, historical records and the existing retry policy.
- [x] Maintainer passed the focused v3 closed-Issue deployed recheck; saved-context
  review confirmed valid empty findings, declared gaps and closure/repair separation.
  Existing knowledge was reused; this is not a cold-build or general accuracy claim.
- [x] Maintainer confirmed the focused v3 offline regression files passed;
  no additional counts, broader integration or CI result is inferred.
- [ ] Final human semantic approval of the remaining reports is pending;
  citation precision and weak History relevance remain improvement notes.
- [ ] Reviewed PR/CI/merge of this first slice.

### Same-branch extension: focused troubleshooting

- [x] Supply read-only Issue URL task history lookup with bounded keyset pages,
  reusing existing detail/events endpoints without submission or freshness I/O.
- [x] Supply delivery-scoped safe diagnostics and committed phase/lifecycle logs;
  preserve cancellation, claim fencing, error classification and finite retries.
- [x] Configure bounded application container log rotation and document export,
  recreation limits and existing API safety boundaries.
- [x] Supply offline concurrency/privacy/lifecycle/API regressions and isolated
  opt-in PostgreSQL lookup acceptance, without schema migrations or paid calls.
- [x] Maintainer reported the troubleshooting tests passed; no exact suite scope
  or additional count was supplied, and assistant execution is not implied.
- [ ] Confirm deployed read-only lookup/events and container log settings;
  no new deployment result has been reported for this extension.

### Remaining release work

- [ ] Representative, multi-source Issue+PR/source retrieval and Agent report evaluation, regression metrics and limits.
- [ ] Observability, task retries/lease recovery, security reviews, API safety boundaries and cost monitoring.
- [ ] Reproducible deployment instructions, migrations, env documentation and safe production defaults.
- [ ] Small Web UI: submit public Issue URL, view progress, citations, report, and applicable human review actions.
- [ ] Public English docs, `README.zh-CN.md`, runnable demo and Apache-2.0 compliance.

## V2 — Private repository support — Future

GitHub App installation and scoped access; private indexing isolation per authorized user/organization; secure credentials and retention; human-approved GitHub write operations. None of these are implicit V1 capabilities.

## Engineering rules and acceptance checks

- Do not confuse branch-local implementation, unit tests, opt-in live API tests, production Worker integration, and merged/deployed state.
- Keep independent DB sessions for concurrent tasks; do not retain DB transactions during slow remote network calls.
- Maintain all M2 Outbox/Claim Token/Lease/fencing and at-least-once semantics when adding Agent execution.
- Graph file composes nodes/edges; RAG/Tool/Analysis business logic remains in modules, with clear typed contracts.
- Security: evidence provenance and model/tool data are untrusted; explicit whitelist, schema, and authorization; no accidental model tool side effects.
- Tests in `tests/` (never `experiments/`), with significant concurrency/transaction/retry/routing/error assertions; paid API/e2e opt-in.
- Feature branches and GitHub PR squash-merge only. Before any new implementation, establish current Git/CI status.
- Keep `README.md`, this Roadmap, public ADR and local development notes synchronized on major acceptance milestones; public docs must not leak private settings.

## Maintenance boundaries and remaining risks

Source index ownership is checked in a completed short transaction before retrieval.
History collection, incremental sync, resumable backfill, detail enrichment and
embedding refresh are separate bounded services. History bodies are truncated to
5,000 characters; PR metadata includes merge/head/base revisions and changed-file
counts, not a diff or a verified issue-to-fix relationship.

Public repository metadata checks now precede issue/history/comment/PR content
requests. Provenance checks and self-issue exclusion do not establish that every
model statement is entailed by its citations. Complete result evidence and actual analysis excerpts are now persisted, with
a whole-Graph deadline and deterministic Markdown rendering. Automatic knowledge
preparation/intake are implemented; checkpoint recovery reduces committed-node recomputation.
Monetary budgets, broad semantic fact verification and elimination of all duplicate
provider calls remain outside the current guarantees.
No HNSW, reranker or replacement task status machine is planned without need.

The source benchmark lives in `tests/fixtures/retrieval/sampleproject.json`;
evaluation code is under `src/maintainer_lens/evaluation/`. The extended history
benchmark labels six issues, adds 34 dynamically fetched distractors, and runs 14
queries. It prints scores without enforcing the historical scores as thresholds;
it does not evaluate PR/comment ranking quality.
