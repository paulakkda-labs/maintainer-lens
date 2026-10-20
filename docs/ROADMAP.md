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
| M1-5 | GitHub Actions continuous integration | **Implemented locally; first GitHub Actions run pending** |
| M2 | Public GitHub Issue ingestion, outbox, API, Docker packaging | **Implementation and Docker E2E verified; PR pending** |
| M3 | Repository knowledge and RAG | Planned |
| M4 | Multi-agent investigation | Planned |
| M5 | Memory and human-in-the-loop | Planned |
| M6 | V1 release readiness | Planned |

M1-5 is being completed within the current M2 feature branch, **before M2
merges**. CI is configured, but it must run successfully on GitHub before
M1-5 is marked complete. The original milestone number is kept for traceability.

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

#### M1-5 — Continuous Integration — Implemented, awaiting GitHub verification

The workflow is defined in `.github/workflows/ci.yml`. Its first real
GitHub-hosted run is still required before this milestone is completed.

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
- [ ] Confirm **both jobs green** on GitHub Actions using the M2 feature branch.

All CI database and broker credentials are confined to disposable containers;
no repository secrets or production infrastructure are needed for these tests.

### M2 — Public GitHub Issue Ingestion and Reliable Delivery

**Status: Implemented and verified with a Docker-hosted end-to-end public Issue
investigation. CI validation and PR merge remain outstanding.**

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
- [ ] Confirm GitHub Actions quality and integration jobs pass.
- [ ] Review the final diff and merge M2 via a squash-merge pull request.
- [ ] Update the private/local project status after merge.

#### Outside this initial M2 slice

- [ ] Support GitHub authentication/tokens and shared cross-worker rate limits.
- [ ] Fetch and persist repository source, documentation, comments, and
      relevant Issue/PR history for retrieval.

### M3 — Repository Knowledge and RAG — Planned

- [ ] Implement source/document/history ingestion and indexing.
- [ ] Design code-aware and document-aware chunking.
- [ ] Generate embeddings and store/search them with pgvector.
- [ ] Provide retrieval with source references and evidence provenance.
- [ ] Evaluate retrieval accuracy and relevance on real issues.

### M4 — Multi-Agent Issue Investigation — Planned

- [ ] Implement LangGraph investigation workflows and specialized agents.
- [ ] Introduce reliable tool calling and structured investigation outputs.
- [ ] Produce reports with traceable claims and supporting evidence.
- [ ] Support asynchronous execution, task progress, and failures.

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
