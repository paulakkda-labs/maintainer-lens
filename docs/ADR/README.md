# Architecture Decision Records

Architecture Decision Records (ADRs) are **public project documents intended for version control**. They explain the context, selected architecture and consequences of important decisions. Commit them to GitHub as part of normal reviewed PRs. Keep local progress notes and private experiments under ignored `.local/`, not here.

**Checkout status (2026-10-28):** This ADR directory and `docs/ARCHITECTURE.md` are currently untracked. Include them in a reviewed commit only after local validation. The maintenance tests have not been executed by the assistant; remote CI and deployment were not checked.

These ADRs were reconstructed on 2026-10-27 from verified historical project documents, the provided implementation modules and user-confirmed development milestones. They are **handoff drafts of historically established decisions** and should be compared against live code and any pre-existing ADRs before merging. The date in filenames is the **documentation date**, not a claim about the original decision date. Preserve any existing ADR numbering/identifiers; resolve potential filename conflicts deliberately.

| Record | Status | Summary |
| --- | --- | --- |
| [2026-10-27-reliable-outbox-and-rabbitmq.md](2026-10-27-reliable-outbox-and-rabbitmq.md) | Accepted (historical) | Transactional Outbox + RabbitMQ + at-least-once messaging |
| [2026-10-27-investigation-lease-fencing.md](2026-10-27-investigation-lease-fencing.md) | Accepted (historical) | Atomic database claims, claim tokens, expiring leases and guarded writes |
| [2026-10-27-versioned-source-and-history-rag.md](2026-10-27-versioned-source-and-history-rag.md) | Accepted (historical) | Commit-pinned source and separate history retrieval; exact pgvector, selected hybrid |
| [2026-10-27-bounded-modular-agent.md](2026-10-27-bounded-modular-agent.md) | Accepted (M4 branch, unmerged status unverified) | Modular LangGraph with bounded evidence-grounded tools |

**Not yet accepted or implemented:** adding a second `services/investigation_runner.py`, replacing existing task status tables, durable LangGraph Checkpointer/HITL, or private-repo access. These must not be retroactively documented as approved decisions.
