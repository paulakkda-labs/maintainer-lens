# MaintainerLens Roadmap

MaintainerLens is an evidence-driven, multi-agent platform for investigating
GitHub issues using repository code, documentation, and development history.

> This roadmap describes planned capabilities. Features are not considered
> available until implemented, tested, and documented.

## Version 1 — Public Repository Investigation

V1 focuses on investigating issues in public GitHub repositories.

### M0 — Project Bootstrap

- [x] Initialize the Python project with uv.
- [x] Establish the FastAPI application.
- [x] Add the initial health check and automated test.
- [x] Configure Ruff.
- [x] Establish the Git repository and development workflow.

### M1 — Backend Infrastructure

Build the infrastructure required for reliable asynchronous investigation
processing.

#### M1-1 — Local Infrastructure

**Status: Completed**

- [x] Configure Docker Compose.
- [x] Deploy PostgreSQL 17 with pgvector.
- [x] Deploy Redis 7.4.
- [x] Deploy RabbitMQ 4.2 with Management.
- [x] Configure persistent volumes and health checks.
- [x] Restrict development service ports to localhost.
- [x] Verify database, cache, and message broker connectivity.

#### M1-2 — PostgreSQL and Database Foundation

**Status: Completed**

- [x] Integrate SQLAlchemy Async.
- [x] Configure asynchronous database connections and sessions.
- [x] Configure Alembic migrations.
- [x] Establish database models and persistence operations.
- [x] Verify database operations and transaction behavior.

#### M1-3 — Redis Integration

**Status: Completed**

- [x] Integrate the asynchronous Redis client.
- [x] Configure Redis connection management.
- [x] Integrate Redis into the application lifecycle.
- [x] Verify Redis connectivity and basic operations.

#### M1-4 — RabbitMQ and Worker Foundation

**Status: Implementation and end-to-end validation completed; PR pending**

- [x] Implement RabbitMQ connection management.
- [x] Configure durable exchanges and queues.
- [x] Implement persistent message publishing.
- [x] Enable publisher confirms and mandatory routing.
- [x] Implement asynchronous message consumption and acknowledgements.
- [x] Implement delayed retry and dead-letter queues.
- [x] Implement investigation task persistence and state transitions.
- [x] Implement atomic task claiming with claim tokens.
- [x] Implement lease expiration, renewal, and ownership validation.
- [x] Implement retryable investigation execution.
- [x] Implement bounded retry attempts and terminal failure handling.
- [x] Establish worker dependency wiring and resource management.
- [x] Add unit and integration tests.
- [x] Validate success, retry, dead-letter, and acknowledgement behavior
      with real PostgreSQL and RabbitMQ.
- [x] Complete code review and merge the M1-4 pull request.

The worker foundation supports dependency injection of investigation
processors. The standalone production worker entry point will be enabled
when the initial investigation processor is implemented in M2.

#### M1-5 — Continuous Integration

**Status: Planned**

- [ ] Configure GitHub Actions.
- [ ] Run Ruff lint and formatting checks in CI.
- [ ] Run automated tests in CI.
- [ ] Verify package installation and application startup.
- [ ] Document the CI workflow.

### M2 — GitHub Integration

- [ ] Integrate the GitHub REST API.
- [ ] Retrieve public repositories, issues, and pull requests.
- [ ] Implement GitHub API authentication and rate-limit handling.
- [ ] Implement asynchronous requests, timeouts, and error classification.
- [ ] Fetch repository source code and documentation.
- [ ] Persist retrieved investigation data.
- [ ] Implement the initial investigation processor.
- [ ] Connect the investigation processor to the worker entry point.

### M3 — Repository Knowledge and RAG

- [ ] Build a repository indexing pipeline.
- [ ] Implement code and document chunking.
- [ ] Integrate embeddings and pgvector.
- [ ] Implement retrieval with traceable source references.
- [ ] Evaluate retrieval quality.

### M4 — Multi-Agent Issue Investigation

- [ ] Build investigation workflows with LangGraph.
- [ ] Implement specialized agents and tool calling.
- [ ] Generate evidence-backed investigation reports.
- [ ] Support asynchronous execution and progress tracking.

### M5 — Memory and Human-in-the-Loop

- [ ] Persist investigation state and checkpoints.
- [ ] Implement human feedback, interrupts, and workflow resumption.
- [ ] Support short-term and long-term memory.
- [ ] Add controls for storing and updating trusted project knowledge.

### M6 — V1 Release Readiness

- [ ] Establish an Agent evaluation dataset.
- [ ] Add observability and reliability checks.
- [ ] Review security and access controls.
- [ ] Document deployment and configuration.
- [ ] Publish English and Chinese documentation.
- [ ] Prepare a reproducible public demo.

## Version 2 — Private Repository Support

Planned capabilities include:

- GitHub App integration and authorization.
- Private repository access.
- Multi-user access control and data isolation.
- Human-approved GitHub write operations.

## Non-goals

MaintainerLens does not aim to replace IDE-based AI coding assistants.
Automatic code editing and pull request generation are not V1 goals.

## Development Principles

- Prioritize correctness, security, and maintainability.
- Keep implementation and documentation consistent.
- Require automated tests for production features.
- Make important agent decisions traceable and reviewable.
- Evaluate agent quality against real-world GitHub issues.
- Use feature branches, pull requests, and squash merges for development.
