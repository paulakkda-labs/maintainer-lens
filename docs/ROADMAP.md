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

- [ ] Configure local infrastructure with Docker Compose.
- [ ] Integrate PostgreSQL, Redis, and RabbitMQ.
- [ ] Configure SQLAlchemy and Alembic.
- [ ] Establish API and background worker foundations.
- [ ] Set up GitHub Actions CI.

### M2 — GitHub Integration

- [ ] Integrate the GitHub API.
- [ ] Retrieve public repositories, issues, and pull requests.
- [ ] Fetch repository source code and documentation.
- [ ] Implement asynchronous ingestion and error handling.

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