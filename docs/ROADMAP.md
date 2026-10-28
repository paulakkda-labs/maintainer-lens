# MaintainerLens Roadmap

> Reviewed against the local checkout on 2026-10-28. Local main is b1d1da0 (M3); feature HEAD is 2836ee5 (M4 base). Query-planning/tool modules and the maintenance changes remain working-tree changes. This review did not fetch remote state, run tests, or verify CI/deployment.

## Product vision

MaintainerLens is an evidence-driven, server-side investigation platform for maintainers of public GitHub repositories. It joins source/documentation and Issue/PR history knowledge with a reliable asynchronous backend, LangGraph reasoning, validated citations, and ultimately human oversight and memory. V1 is read-only on **public** repositories; private authorization, multi-tenant isolation, and authorized write actions belong to V2. It is not a coding-editor replacement or automated patch author.

## Milestone status

| Milestone | Goal | Status as of handoff |
| --- | --- | --- |
| M0 | Bootstrap, project conventions, FastAPI liveness | Completed/merged (historical) |
| M1 | PostgreSQL, Redis, RabbitMQ, reliable Worker, CI | Completed/merged (historical) |
| M2 | Public Issue snapshot HTTP→outbox→RabbitMQ→Worker E2E | Completed/merged, PR #7 (historical) |
| M3 | Repository source/documentation and Issue/PR knowledge, RAG, evaluation | Present on local main at b1d1da0 (#8–#10); remote checks unverified |
| M4 | Evidence-grounded LangGraph investigation, tool calling, reports, Worker integration | **In progress** — Tasks 53–62 tested; **Worker integration not implemented** |
| M5 | Workflow checkpoints, state recovery, durable memory, HITL | Planned |
| M6 | V1 release readiness, evaluation, security, Web UI | Planned |
| V2 | Authenticated private repository and approved GitHub write operations | Future |

**Definition of done:** Implementation + meaningful tests + any required real external verification + project docs + reviewed PR/green CI + squash merge. An isolated unit test success is not proof that the feature is running in the production Worker. The M4 feature branch and current CI need verification in Git.

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
- [x] Historically observed green CI on completed PRs. **Do not extrapolate to the current unmerged branch.**

## M2 — Public GitHub Issue intake and delivery — Complete

- [x] Typed GitHub REST client for public Issues, distinguishing PR objects and classifying rate limits, transient HTTP and network failures.
- [x] Transactional creation of InvestigationTask plus Outbox Event with HTTP `202 Accepted`.
- [x] Independent outbox dispatcher claims events, publishes with RabbitMQ confirms, marks success only after broker confirmation, and safely releases failed deliveries for retry.
- [x] Worker uses existing `src/maintainer_lens/investigation_runner.py` to claim/renew tasks and invoke `services/issue_investigator.investigate_issue()`.
- [x] Guarded IssueSnapshot upsert under matching claim token + valid lease.
- [x] HTTP `GET /investigations/{task_id}` returns status and optional snapshot.
- [x] Local host-mode real GitHub E2E (`encode/httpx#3712`) and Docker end-to-end (`pydantic/pydantic#13781`) success paths.
- [x] Historical PR #7 merged using squash strategy.

**Boundary:** M2 Worker **fetches and saves a snapshot**. It does not automatically run the M4 Graph just because the Agent modules exist in the same package.

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

### M4 implementation already tested, pending production integration

User-confirmed Tasks 53–62 (some subtask IDs earlier than 53 are not fully documented in the provided handoff; verify source history rather than inventing task names):

- [x] History search LangGraph node and source/history TypedDict State.
- [x] Source RAG node and evidence provenance conversion.
- [x] DeepSeek-compatible structured `InvestigationAnalysis`: summary, findings with checked evidence IDs, hypotheses and unanswered questions.
- [x] Conditional ready/more-evidence routing.
- [x] Bounded iterative retrieval, deduplication, early stop on no novel evidence, max rounds.
- [x] Decompose graph: `investigation_graph.py` is a small graph composer, business functions in `agents/investigation/` modules.
- [x] LLM follow-up Query Planner with strict structured output; passed a real DeepSeek Flash test (Task 59).
- [x] Model-selected `search_source` / `search_history` using real function-calling protocol, validated tool schemas/authorization, `tool_call_id` tool results; passed a real DeepSeek API test with **mock retrieval data** (Task 60).
- [x] Integrate model-selected search into LangGraph, retaining complete evidence and doing only selected follow-up search; passed mock graph regression tests (Task 61).
- [x] Add single-tool-step timeout, cancellation correctness and metadata-only observability; passed tests (Task 62).

### M4 remaining — design with existing backend, not a replacement

- [x] Review the local Worker, task runner, Outbox, lease DB code, Agent graph, migrations and tests (2026-10-28).
- [ ] Run and review October 28 maintenance regressions: source-adapter transaction cleanup, processor exception classification, lease-loss routing, poison-message ACK ordering, public visibility, and historical provenance. Code and tests are prepared; no passing result is claimed.
- [ ] Design the integration seam around **existing** `src/maintainer_lens/investigation_runner.py`, `services/issue_investigator.py`, `messaging/handler.py` and the DB state machine. Do **not** add a second investigation status engine.
- [ ] Establish full-graph deadline/result validation in the **appropriate existing owner** or a purpose-specific collaborator only if needed; do not blindly implement the previously proposed Task 63 `services/investigation_runner.py` file.
- [ ] Validate the maintenance error policy against the real Graph when Worker integration is designed. The current runner maps processor failures into bounded retry/permanent rules, preserves lease loss/cancellation, and leaves infrastructure/finalization failures visible; full Graph/Worker integration remains pending.
- [ ] Define guarded persistence for analyzed findings, traceable report and final state. Never mark `succeeded` before necessary DB writes are committed and fenced by the valid claim token.
- [ ] Wire one real public Issue through HTTP→Outbox→RabbitMQ→Worker→IssueSnapshot→LangGraph source/history RAG→structured report→HTTP readback. Explicitly identify mock/live components in tests.
- [ ] Provide report citations that resolve to actual Issue/PR or immutable source commit/line anchors.
- [ ] Complete docs, automated tests and CI, then PR/squash-merge M4.

**M4 non-goals for now:** LangGraph Checkpointer, long-term cross-investigation memory, full HITL interrupt/recovery, and broad multi-agent orchestration; reserve them for M5 once the single workflow is correct.

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
- Keep `README.md`, this Roadmap, public ADR and private Chinese `.local/PROJECT_STATUS.md` synchronized on major acceptance milestones; public docs must not leak private settings.

## Maintenance boundaries and remaining risks

Source index ownership is checked in a completed short transaction before retrieval.
History collection, incremental sync, resumable backfill, detail enrichment and
embedding refresh are separate bounded services. History bodies are truncated to
5,000 characters; PR metadata includes merge/head/base revisions and changed-file
counts, not a diff or a verified issue-to-fix relationship.

Public repository metadata checks now precede issue/history/comment/PR content
requests. Provenance checks and self-issue exclusion do not establish that every
model statement is entailed by its citations. Durable evidence versions, complete
report persistence, whole-Graph time/cost controls and duplicate-computation
mitigation need later feature design. No HNSW, reranker, new status machine or
new report schema is introduced by the maintenance changes.

The source benchmark lives in `tests/fixtures/retrieval/sampleproject.json`;
evaluation code is under `src/maintainer_lens/evaluation/`. The extended history
benchmark labels six issues, adds 34 dynamically fetched distractors, and runs 14
queries. It prints scores without enforcing the historical scores as thresholds;
it does not evaluate PR/comment ranking quality.
