# Architecture Decision Records

Architecture Decision Records (ADRs) are **public project documents intended for version control**. They explain the context, selected architecture and consequences of important decisions. Commit them to GitHub as part of normal reviewed PRs. Keep local progress notes and private experiments under ignored `.local/`, not here.

The initial records were documented on 2026-10-27 from established project decisions. Their filenames retain that documentation date. They are accepted, version-controlled design records; current module responsibilities and integration boundaries are described in the [architecture](../ARCHITECTURE.md).

Keep context, decisions and consequences here. Development branch names, test-session logs, teaching notes and per-task implementation plans belong in local development notes. If a future change replaces an accepted decision, add a superseding record and link it rather than silently rewriting its rationale.

| Record | Status | Summary |
| --- | --- | --- |
| [2026-10-27-reliable-outbox-and-rabbitmq.md](2026-10-27-reliable-outbox-and-rabbitmq.md) | Accepted (historical) | Transactional Outbox + RabbitMQ + at-least-once messaging |
| [2026-10-27-investigation-lease-fencing.md](2026-10-27-investigation-lease-fencing.md) | Accepted (historical) | Atomic database claims, claim tokens, expiring leases and guarded writes |
| [2026-10-27-versioned-source-and-history-rag.md](2026-10-27-versioned-source-and-history-rag.md) | Accepted (historical) | Commit-pinned source and separate history retrieval; exact pgvector, selected hybrid |
| [2026-10-27-bounded-modular-agent.md](2026-10-27-bounded-modular-agent.md) | Accepted | Modular bounded LangGraph, application execution boundary and durable preliminary results |

Checkpoint recovery, HITL and private-repository access remain future design work. The accepted ownership model reuses the existing task runner and status table. The Graph processor is implemented but not connected to the default Worker; that integration must preserve these decisions.
