# Claim-fenced Graph checkpoints under the existing task lifecycle

Status: Accepted. Implementation is supplied; migration/pytest acceptance is pending.
Date: 2026-10-29.

## Context

RabbitMQ deliveries are at least once. The existing Runner claims Tasks and
maintains a renewable, fenced lease; final result and status commits are separate.
Without durable Graph progress, a retry can repeat all retrieval/model work.
Checkpoint persistence must not accept writes from an expired/replaced Worker,
replace Task status, or bypass Alembic with a second driver's setup routine.

LangGraph exposes async Saver operations for snapshots and pending node writes.
Node outputs can be persisted before a snapshot callback completes. Saving only
an application State JSON omits framework scheduling/channel-version state and
cannot establish equivalent recovery semantics.

## Decision

Use the public async `BaseCheckpointSaver` protocol through the existing
SQLAlchemy/asyncpg stack. Register three ORM tables under `db/models` and create
them through one additive Alembic migration:

1. Task-linked frozen execution input and non-secret configuration identity.
2. Full typed checkpoints, including channel values, versions and parent IDs.
3. Ordered pending node writes, owned by the run independently of snapshot
   callback ordering, with SDK regular/special-write deduplication semantics.

The business Task ID is the stable `thread_id`; the claim token is ownership,
not a new thread. Each operation owns a short Session/transaction, locks the Task,
checks fresh database wall time, running status and token, checks frozen
repository/Issue/effective index identity, and rechecks ownership before commit.
No transaction spans a provider request. Task deletion cascades through progress.

Freeze Issue text/language/index and Graph/SDK/model/provider/configuration
identity after snapshot/preparation/binding. Store endpoint hashes, not endpoint
credentials. Key rotation alone remains possible. Incompatible configuration or
corruption fails explicitly; it never triggers an automatic reset or new HEAD.
Graph/prompt semantics changes require an application Graph version increment.

Reuse the existing Graph builder and M4 validation. With no checkpoint, invoke
fresh validated input; otherwise invoke `None`. Use synchronous durability before
the next super-step and Graph return. Revalidate restored identity, evidence,
pending writes and rounds before resumed external work; validate final results
using the existing citation/URL/readiness rules. Serialization disables pickle
and arbitrary imported types. Current support is async ordinary state channels;
DeltaChannel/subgraph reconstruction and administrative pruning/copy are absent.

Each active claimed Graph segment retains 120 seconds; saved search rounds and
existing MQ attempt limits do not reset. Checkpoint I/O is inside that segment.
LangGraph waits for background Saver callbacks on exit, so cancellation first
aborts those operations and the child Graph task, then awaits cleanup before
propagating. Own deadline uses `expired()`; external cancellation, upstream
errors and exception groups retain their meanings. This is cleanup coordination,
not a new task runner or automatic retry system.

Final checkpoint → fenced result commit → original Runner success → Consumer
ACK remains the completion order. Reclaim can reuse a completed checkpoint and
idempotently persist the business result after a crash between those commits.

## Consequences and limits

Committed nodes can be reused across deliveries; unfinished provider requests
may repeat. Preparation before the frozen run is created reuses ready knowledge
but has no exact download/embedding checkpoint. Cancellation can take cleanup
time and cannot guarantee that remote computation/billing stops. There is no
exactly-once or cumulative cross-attempt monetary-budget guarantee.

Full snapshots duplicate some evidence across bounded steps and retain Issue
content; storage/retention must be considered before a larger release. No public
checkpoint-edit/reset API is added. Old completed tasks are not backfilled.

Normal reports do not require human review. Later work may pause at adjustable
application-budget limits for a versioned human decision; it cannot approve
invalid JSON/citations/identity/ownership or exceed provider hard limits. Review
states/endpoints and trusted memory are outside this migration.

Acceptance includes actual compiled Graph/Saver tests, opt-in PostgreSQL
ownership/rollback/reclaim tests and real RabbitMQ Worker cancellation/redelivery
with simulated providers. Supplying these tests does not establish they passed.

References: [LangGraph Saver protocol](https://docs.langchain.com/oss/python/langgraph/checkpointers),
[LangGraph interruption semantics](https://docs.langchain.com/oss/python/langgraph/interrupts),
[architecture](../ARCHITECTURE.md#durable-graph-execution-and-recovery).
