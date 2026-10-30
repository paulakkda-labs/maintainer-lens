# On-demand investigation review and approved budget versions

Status: Accepted.
Date: 2026-10-29.

## Context

Normal investigations, including valid `needs_more_evidence` reports, must remain
automatic. An adjustable application budget should allow an operator to inspect
trusted progress and decide whether to continue instead of immediately losing
access to the work. Waiting must not retain a Worker, claim or lease.

The original frozen Run includes the base output allowance. Mutating it would
invalidate checkpoint identity. Delivery generations also previously counted
user recoveries, which would incorrectly charge budget approvals to that quota.

## Decision

Intercept only typed Source file/byte/chunk budget failures, the preparation
scope's own deadline and analysis generation with `finish_reason=length`.
Graph/tool deadlines, upstream errors, invalid JSON/citations, ownership loss and
mixed exception groups retain existing handling. `length` may describe output
or context exhaustion; an output increase is not a promise that it will resolve
an upstream hard limit.

The existing Runner atomically persists a review request and progress event and
conditionally transitions its active claim to `waiting_review`, releasing the
lease. Only after commit may the message be acknowledged. A duplicate delivery
or URL submission displays the waiting task without approving or dispatching it.
Waiting has no automatic expiry.

Each review freezes typed, non-secret context: reason/observed count, effective
preparation/output budgets, base execution identity, and applicable Run and
checkpoint identities. Only an already committed, fully validated previous
analysis can be offered as a partial result. Incomplete provider content and
failed-node pending writes are never acceptable reports.

Decisions address one review UUID and carry a request-scoped idempotency UUID.
An identical replay returns the stored decision; a competing or stale decision
conflicts. Under a short Task lock, an approval writes its immutable decision,
increments the delivery generation and atomically inserts an Outbox event.
Rejecting ends the task without dispatch. Acceptance of a valid partial analysis
also passes through the normal Worker/result/Runner completion path, so a crash
before status commit remains recoverable and report limitations remain explicit.
The Graph checkpoint itself may remain unfinished after human acceptance.

Allow three budget-increase resumes per Task, independently of three user failure
recoveries. Delivery generation is only a stale-message fence. Backfill the old
user-recovery counter from the former generation value. Preparation approvals use
existing profiles and must increase the actual affected limit without reducing
any effective allowance; `unlimited` requires an explicit selection and only
applies to preparation. Output approvals supply a larger finite integer and may
be bounded by a deployment-configured ceiling. No model name is hardcoded or
automatically discovered; providers remain configured through the existing
`.env` workflow, with model keys kept out of API responses and storage.

Retain the original Run and Task intent budgets. The latest approved review is a
separate effective budget version, bound to the same base configuration. Model,
endpoint, embedding, code or other configuration changes cannot be approved as
budget changes. Source/History indexes retain their existing completeness and
ownership requirements. Preparation before a Run exists can redo uncommitted
work; ready knowledge is still reusable.

## Consequences and acceptance

This adds one registered ORM model and an Alembic migration, review read/decision
endpoints, progress kinds and a waiting status in the existing lifecycle. It adds
no competing runner, credential UI, model registry, long-term memory, scheduled
expiry job or exactly-once external-call guarantee. Review endpoints use the
current single-operator V1 deployment/access model.

Validation includes offline schema/API/real-Graph tests, opt-in PostgreSQL
transaction/lease/idempotency races, and actual API→Outbox→RabbitMQ→default Worker
scenarios with simulated providers. See the [testing guide](../DEVELOPMENT.md)
for execution and infrastructure-isolation rules.

Combined workflow closure acceptance is supplied separately for approved-budget
user recovery, interruption between accepted-result commit and task success,
and historical report retention after a newer Task is rejected. These additional
tests exercise persistence and recovery boundaries. Semantic report quality and
deployed HTTP acceptance remain separate checks. Project memory is not a
prerequisite for this review workflow. Accepting a stage analysis does not certify
it as a reusable project fact.
