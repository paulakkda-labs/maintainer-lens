# Automatic investigation intake and knowledge preparation

- Status: Accepted
- Date: 2026-10-28
- Extends: [Explicit Source selection and derived reports](2026-10-28-source-selection-and-derived-reports.md)

## Context

An end user should submit a public GitHub Issue URL and receive an asynchronous
Markdown investigation without creating an index or discovering its UUID.
The explicit-index API remains useful for controlled investigations. Existing
Task metadata already stores automatic mode, a backend-selected UUID and a
versioned preparation budget. The original Runner owns task status/claims and
must remain the only lifecycle authority.

## Decision

Add `POST /investigations/from-url` for strict canonical Issue URLs and a profile
choice. Preserve `/investigations` as the lower-level explicit-knowledge API.
Derive repository/Issue identity rather than fetching a user-supplied URL.
Snapshot the selected budget with Task/Outbox in the existing transaction;
messages carry automatic intent, never another copy of the configuration.

The existing Processor reads the saved snapshot under its claim, acquires the
Issue snapshot, then invokes an injected preparer before Graph. Worker owns
clients. Source and History share one preparation timeout, independent of the
Graph timeout. Distinguish own expiry with `timeout_scope.expired()` and reject
swallowed deadline cancellation. Preserve upstream errors/cancellation and the
existing delivery retry policy.

Resolve a pinned public repository revision and reuse only an exact compatible
complete index (`investigation-auto-v1`). Build through the existing Source
lease protocol otherwise. Check finite file/byte limits before Blob download
and complete chunk limits before Source embeddings. Spool source/vector content
locally and publish bounded SQL batches atomically. Null aggregate limits bypass
the original in-memory downloader rather than disguising its 8 MiB cap.
Per-file/content, transport and tree-traversal protections remain. Metadata and
physical resource usage still grow with corpus size.

Prepare recent History parents page by page and reuse unchanged embeddings.
Null parent limits page until exhaustion. Keep the established per-page detail
policy, explicitly distinguishing preparation from complete historical sync.

Bind the prepared UUID only after both preparations complete and Source metadata
is validated. Lock the existing Task and check fresh database time/token/lease;
binding is idempotent for the same UUID and cannot change to another selection.
A retry with a binding revalidates/reuses it without new HEAD selection or
preparation. Failed attempts before binding can leave reusable knowledge.

Result write and GET compare the effective UUID: prepared for automatic mode,
requested otherwise. Preserve both identities in task details. Keep result JSONB
version 1 and the original Graph contracts/budgets. Generate Markdown from saved
validated analysis/context without an extra LLM request.

## Consequences

- No new table, migration, task status machine, runner or settings system.
- Default preparation uses 500 selected files, 8 MiB, 2,000 chunks, three History
  parents and 900 seconds. Other profiles are discoverable; unlimited applies to
  preparation only. Graph stays at three rounds/120 seconds and tools at 60 seconds.
- The first run can download and embed repository knowledge. Retries are bounded
  by existing delivery policy but may repeat paid requests; this is not monetary
  accounting or exactly-once provider execution.
- Finite Source over-budget failures are explicit, without partial ready indexes.
  History parent limits define a selected work scope; bounded detail enrichment
  does not establish complete comments or verified Issue-to-fix relationships.
- Temporary files close/remove on cancellation; failed index writes roll back.
  No database transaction spans GitHub, embedding, LLM or broker calls.
- Deleting selected knowledge can make retries fail; UUID retention is independent
  of index retention, with no silent fallback.
- Automatic cold-start tests must exercise the actual builder/Worker. Paid tests
  are independently opt-in, limited to a small public repository with hard request
  and input caps. Supplying tests does not establish they passed or prove model
  conclusions. The maintainer confirmed the bounded automatic cold-start smoke
  passed on 2026-10-29; M4 still needs report review and reviewed green CI/merge.
- Checkpoint recovery/HITL/memory remain M5; a product Web UI remains M6.
