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
| [2026-10-28-source-selection-and-derived-reports.md](2026-10-28-source-selection-and-derived-reports.md) | Accepted | Explicit Source identity across delivery/storage, bounded Worker tools and derived reports |
| [2026-10-28-automatic-investigation-preparation.md](2026-10-28-automatic-investigation-preparation.md) | Accepted | URL intake, persisted preparation budgets, complete Source reuse/build and fenced prepared identity |

| [2026-10-29-claim-fenced-graph-checkpoints.md](2026-10-29-claim-fenced-graph-checkpoints.md) | Accepted | Task-linked frozen execution, async SQLAlchemy Saver and claim-fenced recovery |

The Worker supports automatic preparation, explicit knowledge selection and
checkpoint recovery. GET derives Markdown without extra inference. The original
Runner, task status, claim fencing and ACK semantics remain authoritative. M4 is
merged through PR #14. New checkpoint migration/test acceptance is pending; HITL,
trusted memory and private-repository access remain future work.
