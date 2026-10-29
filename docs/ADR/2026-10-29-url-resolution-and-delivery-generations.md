# URL resolution, scoped freshness and delivery generations

Status: Accepted. Maintainer reports local tests passed and services started; implementation merged in PR #16.
Date: 2026-10-29.

## Context

Users submit an Issue URL to obtain a report. Repeating the URL should locate
active work, recover eligible failed work or reuse a report after a bounded
freshness check. A new submission must not always discard the practical benefit
of checkpoints. The original Task lifecycle, claim fencing and Outbox remain
authoritative; no HTTP call may run under a database row lock.

A failed Task can have old RabbitMQ deliveries in flight. Reopening it without a
delivery identity would allow an old message's retry budget or contents to control
new work. The former `(task_id, event_type)` Outbox key also cannot represent a
new durable publication for the same investigation.

## Decision

Resolve automatic URL intent by canonical repository, Issue and persisted
preparation-budget snapshot. Serialize decisions on a URL lookup row, with
separate pointers for current work and the last successful report. Adopt eligible
pre-feature automatic tasks; keep the explicit low-level create-new API intact.

Return pending/running work without duplicate publication. For a terminal
candidate, claim a 30-second check lease, commit, then observe GitHub outside SQL
with a 15-second metadata deadline. Re-lock and verify token, expiry, Task and
generation before applying the decision. Concurrent callers share the check;
expired/replaced observations cannot overwrite newer decisions.

Freshness covers Issue title/body/state/updated_at, default-branch commit and
recorded execution identity. It does not discover all linked/unlinked PR or
discussion changes. Changes create new investigations, preserving the old report
and frozen checkpoint inputs. Verification failure returns a saved report as
`unknown`; it never establishes current applicability. Markdown includes the
freshness warning so exporting it does not lose this qualification.

GitHub open/closed state has display priority and is stated separately from
technical repair evidence. A closed Issue is not automatically described as
independently verified code repair.

Permit at most three user recoveries of one Task, requiring a recorded retryable
failure and compatible inputs/configuration. Atomically change failed → pending,
increment `execution_generation`, add a queued event and insert an Outbox row.
Outbox uniqueness includes generation. Nonzero generations are included in MQ
bodies; zero remains backward-compatible. Claim SQL compares the expected
generation. Stale deliveries return without executing; future generations fail.
The existing per-cycle MQ retry policy and Graph budgets remain unchanged.

Persist safe Task events for preparation/node phases, round, generation and
processing errors. Each processing write checks current claim/lease under a short
Task lock. Lifecycle events share the Runner's status transaction. The original
Runner remains the sole lifecycle owner. Expose bounded event pages and latest
progress/error fields; historical reports are separate from current artifacts.
Allowlist error descriptions rather than persist raw exception/provider text.

## Consequences

The API can return existing work or a report without repeated embeddings/model
calls. Metadata checks still consume GitHub requests and are scoped observations,
not a perpetual correctness guarantee. A successful `needs_more_evidence` report
remains a legitimate reusable result.

The lookup's report anchor preserves access when a later investigation fails.
Legacy failures without a retryability record are not silently recovered. Version
changes intentionally create new Tasks rather than reinterpret old checkpoints.

Progress events add bounded per-step writes; a crash may leave only the last
committed phase. Events are not a second status machine, frontend push transport,
review workflow or long-term memory. There is no exactly-once provider guarantee.

Apply Alembic revision `0ab5e7f9c306` and restart matching API/Dispatcher/Worker
code before enabling recovery generations. Old Workers cannot parse new-generation
messages. Downgrade refuses when generations exist instead of deleting evidence
of prior recovery cycles.

Acceptance includes offline API/Graph/decision contracts, real PostgreSQL races,
lease/rollback/generation tests and API→Outbox→MQ→default Worker recovery with
simulated HTTP providers. No paid call is required by the new suites.
