# MaintainerLens Roadmap

> Updated 2026-10-28. Earlier History-only smoke, Source metadata and task-schema tests passed per maintainer feedback. Source propagation, bounded production tools and derived reports are implemented with new tests awaiting execution. Bounded Source+History live smoke passed per maintainer feedback; preparation schema and selectable budget snapshots are staged, with contracts/API readback implemented. Broader quality acceptance and CI/review remain; implementation does not imply deployment.

## Product vision

MaintainerLens is an evidence-driven, server-side investigation platform for maintainers of public GitHub repositories. It joins source/documentation and Issue/PR history knowledge with a reliable asynchronous backend, LangGraph reasoning, validated citations, and ultimately human oversight and memory. V1 is read-only on **public** repositories; private authorization, multi-tenant isolation, and authorized write actions belong to V2. It is not a coding-editor replacement or automated patch author.

## Milestone status

| Milestone | Goal | Status |
| --- | --- | --- |
| M0 | Bootstrap, project conventions, FastAPI liveness | Completed/merged (historical) |
| M1 | PostgreSQL, Redis, RabbitMQ, reliable Worker, CI | Completed/merged (historical) |
| M2 | Public Issue snapshot HTTP→outbox→RabbitMQ→Worker E2E | Completed/merged, PR #7 (historical) |
| M3 | Repository source/documentation and Issue/PR knowledge, RAG, evaluation | Completed and merged (#8–#10); evaluation limits below |
| M4 | Evidence-grounded LangGraph investigation, tool calling, reports, Worker integration | **In progress** — Graph application/results/readback merged (#11); History-only Worker smoke passed per maintainer feedback; Source/tool/report integration implemented, new acceptance suites and quality closure pending |
| M5 | Workflow checkpoints, state recovery, durable memory, HITL | Planned |
| M6 | V1 release readiness, evaluation, security, Web UI | Planned |
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

**Historical boundary:** M2 delivered snapshot-only processing. The current M4 Worker explicitly invokes the Graph processor with prepared History, optional explicit Source and bounded follow-up; legacy succeeded tasks are not backfilled.

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

### Expanded Source/tools/report integration implemented, acceptance pending

- [x] Carry explicit UUID through API, atomic Task/Outbox, publisher, message and
  Worker; compare repository/Issue/index at claimed preflight, fenced write and decode.
- [x] Validate requested Source metadata before external work; building uses
  existing bounded retries, failed/missing/foreign/incompatible selections fail.
- [x] Bind Tool Runner with up to three rounds, 120-second Graph and 60-second
  tool deadlines; expose Source only for explicit index requests. Stop duplicates
  and non-novel searches without unnecessary analysis. Reject swallowed tool expiry.
- [x] Derive bilingual Markdown reports from validated saved analysis/context,
  preserving insufficiency, citations and literal untrusted text, without new LLM calls.
- [x] Supply default offline regressions and opt-in real PostgreSQL/MQ full-chain
  tests, plus bounded paid Source+History smoke with self-prepared fixture knowledge.
- [ ] Maintainer executes expanded regressions and real-infrastructure/full-chain tests.
- [x] Maintainer-confirmed bounded Source+History paid smoke passed, including
  real preparation fixtures, POST/Outbox/default Worker and GET report checks.
- [ ] Review actual conclusions/citations on representative Issues; IDs/URLs alone
  do not prove truth.
- [x] Stage automatic-preparation mode and backend-selected Source UUID in the
  Task model/migration with database mode constraints; upgrade/tests pending.
- [x] Add small/standard/large/unlimited preparation choices, strict versioned
  snapshots, profile discovery and submit/readback contracts; keep Graph budgets separate.
- [ ] Implement automatic preparation using the stored budget snapshot: default
  Source 500 files / 8 MiB / 2,000 chunks / 900 seconds and three History parents;
  support unlimited task-preparation limits without disabling transport/content/
  Graph controls. Reuse exact compatible ready indexes; keep live tests small.
- [ ] Implement the product intake layer: Issue URL parsing and bounded backend-managed
  knowledge preparation/selection, without requiring user-supplied index UUIDs.
- [ ] Complete final quality review, documentation reconciliation, CI and reviewed PR.

Do not introduce a second task runner or status machine. Preserve existing
claims/leases, fenced required writes, bounded retries and ACK ownership.
M4 remains open until its production integration and acceptance criteria are met.
Checkpointer, cross-run memory, HITL and broad multi-agent orchestration remain M5.

### PR and M5 entry criteria

A staged M4 PR can land the implemented Worker/Source/tools/reports and budget
contracts after the revised migration, relevant regressions and CI/review pass.
Merging that slice does not complete M4. Closing M4 additionally requires URL-only
submission, backend automatic preparation using persisted limits, fenced selected
identity/retry behavior, end-to-end acceptance from unprepared knowledge and a
focused report/citation quality review. Finish documentation and reviewed green-CI
PR(s), then squash merge and synchronize main before starting M5 work. The Web UI
remains M6 and is not an M4 gate. No calendar estimate or unobserved CI status is
implied by this checklist.

## M5 — Durable workflow and human review — Planned

- [ ] Define checkpoint storage and LangGraph `thread_id` usage linked to existing task identity, **without conflating** graph state with business status.
- [ ] Resume after interruption; measure duplicate work when RabbitMQ redelivers and prevent unsafe duplicate effects.
- [ ] Support human-in-the-loop interrupts, review, approvals, and explicit investigation corrections.
- [ ] Persist trusted, evidence-linked long-term knowledge with provenance, change/retention policy, and controls; do not store unverified model guesses as facts.
- [ ] Define trustworthy short-term investigation context and bounded prompt assembly; Chat Completions does not automatically preserve prior calls.

## M6 — V1 release readiness — Planned

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
preparation/intake, monetary budgets, semantic fact verification and duplicate-computation mitigation remain future work.
No HNSW, reranker or replacement task status machine is planned without need.

The source benchmark lives in `tests/fixtures/retrieval/sampleproject.json`;
evaluation code is under `src/maintainer_lens/evaluation/`. The extended history
benchmark labels six issues, adds 34 dynamically fetched distractors, and runs 14
queries. It prints scores without enforcing the historical scores as thresholds;
it does not evaluate PR/comment ranking quality.
