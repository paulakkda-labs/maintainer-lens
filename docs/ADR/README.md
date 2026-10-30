# Architecture Decision Records

[Documentation](../README.md) · [Current architecture](../ARCHITECTURE.md)

These public records explain important architectural choices, their context
and consequences. They belong in version control. Current operating instructions
live in the other guides; task diaries and individual acceptance sessions do not
belong here.

| Record | Status | Decision |
| --- | --- | --- |
| [Reliable Outbox and RabbitMQ](2026-10-27-reliable-outbox-and-rabbitmq.md) | Accepted | Atomic submission and confirmed at-least-once delivery |
| [Investigation lease fencing](2026-10-27-investigation-lease-fencing.md) | Accepted | Claims, renewable leases and stale-worker write protection |
| [Versioned Source and History RAG](2026-10-27-versioned-source-and-history-rag.md) | Accepted | Commit-pinned source and separately scoped historical retrieval |
| [Bounded modular agent](2026-10-27-bounded-modular-agent.md) | Accepted | Focused Graph composition and bounded execution |
| [Source selection and derived reports](2026-10-28-source-selection-and-derived-reports.md) | Accepted | Trusted index identity and reports derived from validated results |
| [Automatic investigation preparation](2026-10-28-automatic-investigation-preparation.md) | Accepted | URL intake, persisted budgets and compatible knowledge reuse/build |
| [Claim-fenced Graph checkpoints](2026-10-29-claim-fenced-graph-checkpoints.md) | Accepted | Frozen execution and durable progress under task ownership |
| [URL resolution and delivery generations](2026-10-29-url-resolution-and-delivery-generations.md) | Accepted | Freshness, arbitration and controlled user recovery |
| [On-demand investigation review](2026-10-29-on-demand-investigation-review.md) | Accepted | Lease-free waiting and explicit, idempotent human decisions |

Filenames retain their documentation dates. Keep the original rationale when
behavior evolves: a replacing decision should add a superseding record and link
it, rather than rewriting historical reasoning. An accepted decision is not proof
that every possible deployment or provider has been validated.
