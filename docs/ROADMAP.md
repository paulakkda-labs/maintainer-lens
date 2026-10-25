# MaintainerLens Roadmap

MaintainerLens is an evidence-driven GitHub issue investigation platform. The
long-term goal is a reliable, server-side, multi-agent system grounded in
repository code, documentation, issues, and pull-request history.

> This document distinguishes implemented capabilities from planned work.
> Passing local unit tests does not imply that opt-in integration tests or
> deployment checks have run.

## Milestone overview

| Milestone | Scope | Status |
| --- | --- | --- |
| M0 | Project bootstrap | **Completed** |
| M1-1 | PostgreSQL, Redis, RabbitMQ infrastructure | **Completed** |
| M1-2 | SQLAlchemy Async and Alembic foundation | **Completed** |
| M1-3 | Async Redis integration | **Completed** |
| M1-4 | RabbitMQ and reliable worker foundation | **Completed and merged** |
| M1-5 | GitHub Actions continuous integration | **Completed and merged** |
| M2 | Public GitHub Issue ingestion, outbox, API, Docker packaging | **Completed and merged** |
| M3 | Repository knowledge and RAG | **In progress** — source indexing/retrieval and initial evaluation verified; Issue/PR history and broader evaluation pending |
| M4 | Multi-agent investigation | Planned |
| M5 | Memory and human-in-the-loop | Planned |
| M6 | V1 release readiness | Planned |

M1-5 and M2 have been completed and merged. M3 repository indexing has been
verified with a live GitHub-to-pgvector test and merged. M3 source retrieval
and initial evaluation (Tasks 28–40) have been implemented and live-tested on
`feat/repository-vector-search`; that feature PR is not yet merged. M3 remains
open until historical Issue/PR knowledge retrieval and broader multi-source
evaluation are complete. Connecting those services to the Investigation
Worker belongs to **M4 (LangGraph)**, not M3.

## V1 — Public Repository Investigation

V1 initially supports GitHub **public** repositories and read-only
investigation. Private repositories and GitHub write operations are V2 scope.

### M0 — Project Bootstrap — Completed

- [x] Initialize the Python/uv project using a `src` layout.
- [x] Establish FastAPI, a liveness endpoint, pytest, and Ruff.
- [x] Adopt Apache-2.0 licensing and a pull-request/squash-merge workflow.
- [x] Establish the initial roadmap and Git repository.

### M1 — Backend Infrastructure

#### M1-1 — Local Infrastructure — Completed

- [x] Provide PostgreSQL 17 with pgvector, Redis 7.4, and RabbitMQ 4.2.
- [x] Provide persistent named volumes and infrastructure health checks.
- [x] Bind development service ports to `127.0.0.1`.
- [x] Verify initial database, Redis, and RabbitMQ connectivity.

#### M1-2 — PostgreSQL Foundation — Completed

- [x] Integrate SQLAlchemy Async engines, sessions, and transaction boundaries.
- [x] Configure Alembic migrations and database models.
- [x] Test persistence, rollback, and important database constraints.

#### M1-3 — Redis Integration — Completed

- [x] Configure the asynchronous Redis client and resource lifecycle.
- [x] Verify Redis connectivity and operations.
- [x] Merge the Redis integration pull request.

#### M1-4 — RabbitMQ and Worker Foundation — Completed and merged

- [x] Configure durable RabbitMQ exchange/queue topology.
- [x] Add persistent messages, mandatory routing, publisher confirms, and ACKs.
- [x] Implement delayed retry queues and dead-letter handling.
- [x] Persist investigation tasks and enforce state transitions.
- [x] Implement atomic claims, ownership tokens, leases, and lease renewal.
- [x] Protect task finalization against stale workers.
- [x] Implement retryable execution and bounded terminal failure behavior.
- [x] Validate core success, retry, dead-letter, and ACK paths with real
      PostgreSQL and RabbitMQ integration tests.
- [x] Review and merge the M1-4 pull request.

#### M1-5 — Continuous Integration — Completed and merged

The workflow is defined in `.github/workflows/ci.yml`. Both jobs were
verified green on GitHub Actions during M2 development. Feature pushes, pull
requests, and updates to `main` trigger CI according to the workflow.

- [x] Configure GitHub Actions for feature branch pushes, pull requests,
      updates to `main`, and manual dispatch.
- [x] Add Python 3.12 and locked `uv` dependency installation.
- [x] Configure Ruff lint and formatting checks.
- [x] Configure the default pytest suite and Python distribution build.
- [x] Configure a separate integration job with disposable PostgreSQL
      (pgvector), Redis, and RabbitMQ service containers.
- [x] Configure Alembic migrations, FastAPI lifespan/liveness startup check,
      and opt-in PostgreSQL/RabbitMQ/Worker end-to-end tests.
- [x] Document CI setup and validation in the README.
- [x] Confirm **both jobs green** on GitHub Actions using the M2 feature branch.

All CI database and broker credentials are confined to disposable containers;
no repository secrets or production infrastructure are needed for these tests.

### M2 — Public GitHub Issue Ingestion and Reliable Delivery

**Status: Completed, verified with a Docker-hosted public-Issue end-to-end
test, and merged through PR #7.**

This milestone delivers a complete **public-Issue snapshot** vertical slice,
not repository indexing or agent-generated investigation reports.

#### GitHub ingestion and task execution

- [x] Implement a typed GitHub REST API Issue client and reject PR objects.
- [x] Manage shared async HTTPX clients, timeouts, HTTP error classification,
      and GitHub primary/secondary rate-limit cases.
- [x] Apply bounded retries, delayed retry/dead-letter routing, and
      per-message `not_before` behavior.
- [x] Save GitHub Issue snapshots with traceable source URLs and timestamps.
- [x] Require the active claim token and valid lease before snapshot writes.
- [x] Connect the production Worker entry point to the Issue investigator.

#### Transactional Outbox and HTTP API

- [x] Atomically create the investigation task and outbox event in PostgreSQL.
- [x] Claim unpublished outbox events using row locking and delivery leases.
- [x] Publish with RabbitMQ confirms; mark published only after confirmation.
- [x] Handle failed publication with safe claim release/retry delays.
- [x] Run an independent outbox dispatcher.
- [x] Implement `POST /investigations` (HTTP 202).
- [x] Implement `GET /investigations/{task_id}` with status and optional snapshot.
- [x] Verify a live end-to-end Issue request through local Python processes,
      real PostgreSQL, RabbitMQ, and the GitHub API.

#### Packaging, testing, and delivery

- [x] Add a shared application Docker image and separate API, dispatcher,
      and worker Compose services.
- [x] Add a one-shot Alembic migration service with startup dependencies.
- [x] Preserve existing PostgreSQL/Redis/RabbitMQ named volumes and localhost
      port bindings.
- [x] Successfully build/start the stack on a Docker-capable Mac: infrastructure
      and API healthy, dispatcher/worker running, migration job exited with 0.
- [x] Resolve the original RabbitMQ vhost mismatch by using `RABBITMQ_VHOST`
      consistently in Compose and existing environment configuration.
- [x] Split the oversized worker integration test module by responsibility.
- [x] Re-run Ruff and default pytest after refactoring: **128 passed,
      41 skipped** (opt-in integration tests not included in this count).
- [x] Document installation, lifecycle commands, diagnostics, and local tests.
- [x] Confirm **Docker-hosted** HTTP → Outbox → RabbitMQ → Worker → GitHub →
      Snapshot → HTTP success with `pydantic/pydantic` Issue #13781.
- [x] Confirm GitHub Actions quality and integration jobs pass.
- [x] Review the final diff and merge M2 via a squash-merge pull request (#7).
- [x] Update the private/local project status after merge.

#### Outside this initial M2 slice

- [x] Add optional GitHub token support for repository ingestion in M3.
- [ ] Coordinate GitHub rate limits across workers.
- [ ] Fetch and index Issue comments and relevant Issue/PR history for retrieval.

### M3 — Repository Knowledge and RAG — In progress

**Repository indexing foundation — Completed and merged (Tasks 1–27).**

- [x] Resolve public GitHub repository revisions and capture commit/tree SHA identities.
- [x] Traverse Git trees, download source blobs, validate content, and apply bounded download concurrency.
- [x] Implement syntax-aware source chunking for Python, JavaScript/TypeScript, Go, and Java, plus heading-aware Markdown chunking.
- [x] Provide configurable, OpenAI-compatible document and query embedding clients.
- [x] Persist versioned repository indexes, chunks, metadata, and embeddings in PostgreSQL with pgvector.
- [x] Support configurable embedding dimensions, transactional index writes, and database uniqueness checks.
- [x] Reuse ready indexes before paid embedding requests for the same repository revision and embedding/chunker configuration.
- [x] Use PostgreSQL build leases, claim tokens, heartbeat renewal, and transaction-level fencing to reject stale workers.
- [x] Verify real PostgreSQL concurrency and transactional behavior with opt-in integration tests.
- [x] Verify live end-to-end indexing of `pypa/sampleproject`: **13 stored chunks**, **1024-dimensional embeddings**, **1 initial embedding request**, **0 additional requests on reuse**, and **no remaining build lease**.
- [x] Merge the completed repository ingestion and vector indexing implementation through the feature-branch / squash-merge workflow.

**M3-4/M3-5 — Source retrieval and initial evidence evaluation — Implemented and verified (Tasks 28–40); PR pending.**

- [x] Implement query embedding and top-k exact pgvector cosine similarity search scoped to one ready repository index.
- [x] Return attributable source evidence with revision identity, path, line range, and chunk metadata.
- [x] Implement PostgreSQL `ILIKE` literal-keyword retrieval with identifier/path/content ranking, including wildcard escaping and index isolation.
- [x] Implement deterministic reciprocal rank fusion (RRF) and semantic + optional keyword hybrid retrieval.
- [x] Add conservative code-identifier query analysis and a unified natural-language repository search entry point.
- [x] Verify live Chinese/English semantic and hybrid retrieval with real GitHub, PostgreSQL, and an embedding provider.
- [x] Implement Recall@K and MRR@K metrics, a manually labeled pinned-commit dataset, and evidence-anchor-to-chunk-key resolution.
- [x] Add self-contained PostgreSQL integration tests with isolated data setup and cleanup for anchor resolution.
- [x] Compare semantic vs. automatic hybrid retrieval on eight real end-to-end queries against `pypa/sampleproject` at commit `621e4974ca25ce531773def586ba3ed8e736b3fc`: both scored **Recall@5 = 1.000** and **MRR@5 = 1.000**, with all labeled evidence at rank 1.

**M3-6 — Historical Issue/PR knowledge — Not started**

- [ ] Fetch relevant public GitHub Issues, Issue comments, PRs, PR comments or
      reviews as needed, and change-file metadata using bounded API access.
- [ ] Persist stable GitHub object identifiers, provenance URLs, timestamps,
      and content needed for reproducible retrieval. Handle mutable history
      using update/capture metadata rather than treating it as immutable code.
- [ ] Prepare searchable text/chunks and versioned embeddings or another
      justified retrieval representation; reuse existing infrastructure where
      appropriate without forcing history into source-only index identities.
- [ ] Support isolated, idempotent ingestion/update and traceable history
      search independently of the investigation Worker.
- [ ] Record confirmed Issue–PR relationships, relevant changed files, and
      verifiable fix/source links; do not invent inferred relationships.
- [ ] Cover important failures, pagination, duplicate/update behavior,
      transaction boundaries, cleanup, and source attribution with tests.

**M3-7 — Multi-source retrieval evaluation and M3 closeout — Not started**

- [ ] Build a modest but representative manually labeled evaluation set for
      complex Issues, source files, comments, related PRs, and multi-evidence
      answers; keep known limitations explicit.
- [ ] Compare source and history retrieval quality on pinned/reproducible
      fixtures and selected opt-in real tests, checking Recall@K, MRR@K, and
      evidence provenance.
- [ ] Fix demonstrated correctness/retrieval problems, avoiding speculative
      refactors. HNSW, reranking, and separate search engines remain optional
      and evidence-driven rather than mandatory M3 features.
- [ ] Pass relevant quality/integration checks, merge the history/evaluation
      work through PRs, and mark **M3 Completed** only after these items land.

**Boundary with M4:** M3 supplies independently callable, source-attributed
repository and history retrieval. It does **not** integrate them into the
Investigation Runner. M4 will wrap the retrieval services as LangGraph tools,
orchestrate the investigation, and connect the workflow to the existing
RabbitMQ Worker and durable task lifecycle. Avoid a temporary hand-coded
Worker search pipeline that would duplicate the later Agent workflow.

The eight-query `pypa/sampleproject` benchmark covers only **two distinct
source targets**. Both strategies achieved Recall@5 = 1.000 and MRR@5 = 1.000
with all target chunks ranked first, validating the smoke-test path but not
establishing general effectiveness or hybrid superiority. V1 retains exact
pgvector cosine search; no HNSW/reranker is needed without evidence. A
separate indexing queue/worker remains deferred until load warrants it.

### M4 — Multi-Agent Issue Investigation — Planned

Start **only after M3-6 and M3-7 are complete**. Begin with one reliable
LangGraph investigation workflow, not a complex multi-agent graph. Keep the
existing M2 task delivery and lease/claim-token ownership model.

- [ ] Define investigation state, nodes, routing, iteration bounds, and
      structured outputs in LangGraph; expand to multiple agent roles only
      when the single workflow is reliable.
- [ ] Add thin tool adapters around existing M3 source search and Issue/PR
      history search; do not reimplement those retrieval services in the graph.
- [ ] Use the persisted GitHub Issue snapshot and collected evidence to
      distinguish verified facts, hypotheses, missing evidence, and citations.
- [ ] Integrate LangGraph execution with the Investigation Runner/Worker,
      including cancellation, lease ownership, bounded retries, error
      propagation, task state, and result persistence.
- [ ] Generate an evidence-backed investigation report and verify citations
      point to actual source/history objects.
- [ ] Validate a real public Issue through the integrated workflow.

Persistent checkpointing, long-term memory, and human approval mature in M5;
M4 first establishes the workflow and its integration boundaries.

### M5 — Memory and Human-in-the-Loop — Planned

- [ ] Persist agent state and checkpoints for workflow resumption.
- [ ] Support interrupts, reviewer feedback, and HITL approval.
- [ ] Introduce short-term and long-term investigation memory.
- [ ] Define trusted project knowledge update and retention policies.

### M6 — V1 Release Readiness — Planned

- [ ] Build representative issue-investigation and agent evaluation datasets.
- [ ] Strengthen reliability, observability, and security boundaries.
- [ ] Provide reproducible deployment and configuration documentation.
- [ ] Publish English documentation and a Chinese README.
- [ ] Deliver a small usable Web UI for submitting public Issue URLs,
      viewing status and evidence-backed reports, and reviewer actions.
- [ ] Prepare an end-to-end public demo.

## V2 — Private Repository Support — Planned

- GitHub App installation, authentication, and authorization.
- Private repository access and multi-user data isolation.
- Human-approved GitHub write operations.

## Non-goals

MaintainerLens is not intended to replace Copilot, Cursor, Claude Code, or
other IDE-based editing assistants. Automatic source editing and PR generation
are not V1 goals. The focus is server-side investigation, evidence provenance,
reviewable decisions, and knowledge accumulation.

## Engineering principles

- Prioritize correctness, maintainability, and clear transaction boundaries.
- Keep production features covered by meaningful tests for risky paths:
  concurrency, retries, leases, idempotency, and atomicity.
- Do not conflate local unit tests with real integration/e2e coverage.
- Document only behavior that has been implemented and verified.
- Use feature branches, reviewed PRs, and squash merges; never push to `main`
  directly.
