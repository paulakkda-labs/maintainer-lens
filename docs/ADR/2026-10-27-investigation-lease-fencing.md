# ADR: Database claims, leases and fencing for investigation ownership

- **Status:** Accepted in the established M1–M2 implementation (reconstructed 2026-10-27).
- **Scope:** Long-running investigation execution and safely persisted effects.

## Context

At-least-once RabbitMQ delivery means two workers may see the same task, and a previously assigned worker can resume after its lease expires. An ordinary process-local lock does not establish ownership across processes or survive crashes. A stale worker must not mark a newly reclaimed task successful or overwrite its data.

## Decision

Store the investigation task lifecycle in PostgreSQL (`pending`, `running`, `succeeded`, `failed`). Use atomic conditional SQL updates to claim pending/expired tasks. Every claim receives a new opaque `claim_token` and an expiring lease. Compare status, task ID, token and **PostgreSQL server time** when renewing, releasing, failing, marking success or writing snapshot data. `src/maintainer_lens/investigation_runner.py` executes the processor alongside a lease-renewal coroutine using Python `asyncio.TaskGroup`; losing lease ownership raises an error. Messaging handler owns retry/dead-letter policy and Consumer owns ACK.

## Consequences

- **Benefits:** Duplicate delivery does not normally result in two valid simultaneous finalizers; expired jobs can be reclaimed; stale writers are fenced; ownership survives worker restarts.
- **Trade-offs:** Requires careful timeout/renewal intervals, DB availability and exception/cancellation handling. A lease is not a substitute for idempotent external API calls.
- **Critical integration constraint:** Existing `investigation_runner.py` must be reused when attaching LangGraph. Creating a parallel runner with a competing status machine or marking success without guarded persistence violates this decision.
- **October 28 maintenance (tests pending):** Processor exceptions are classified recursively across nested `ExceptionGroup` leaves. Existing typed failures keep their policy; invalid data and non-transient HTTP errors are permanent, while timeouts/network/transient HTTP/unknown processor failures use the existing finite retry budget. A permanent member makes an ordinary processing group terminal; retryable groups retain the latest supplied retry deadline.
- **Ownership failures:** Lease-loss leaves bypass processing classification. The handler rereads task status after plain or pure grouped lease loss and then uses existing duplicate/retry/dead-letter rules. It never releases another owner's claim. Mixed lease/other groups propagate. Heartbeat DB errors, finalization failures and external cancellation remain visible; cancellation does not mark success or release ownership.
- **Persistence boundary:** Snapshot and final task status commit separately. A saved snapshot may precede a failed finalization. Future required report writes must also be fenced and committed before the runner reports success; reports are not implemented by this maintenance.
- **Validation:** `tests/test_investigation_error_classification.py`, `tests/messaging/test_delivery_safety.py` and the opt-in PostgreSQL `tests/integration/test_investigation_error_recovery.py` exercise these contracts. The developer has not yet supplied results for this revision.

## Source anchors

`src/maintainer_lens/investigation_runner.py`, `db/investigation_tasks.py`, `db/issue_snapshots.py`, `messaging/handler.py`, `messaging/consumer.py`, related integration tests. The reviewed code uses a 15-minute lease and five-minute default renewal interval. Task state SQL uses statement_timestamp(); snapshot locking uses clock_timestamp(). Neither setting changed in this maintenance.
