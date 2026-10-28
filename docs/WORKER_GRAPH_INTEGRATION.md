# Worker–Graph Integration Contract

**Status: Shared schemas/storage/processor verified by the developer; HTTP
result readback implemented in the working tree (2026-10-28).** The developer
reports the result table was created and processor tests passed. Readback tests
are supplied but await developer execution. Default Worker wiring remains
future work.

Task 63 is implemented in the working tree. Before shared-schema extraction,
the developer confirmed all tests passed after the three-round fixture
correction; aggregate counts and logs were not provided. The developer also
confirmed the extraction's tests passed, then reported the result table created
and persistence tests passed, followed by confirmation that processor tests
passed. Aggregate counts and optional-test logs were not supplied. These results
do not cover the new HTTP slice or establish a live Worker–Graph E2E, remote CI,
merge or deployment.

## 1. Verified starting points

| Existing component | Verified contract |
| --- | --- |
| [Worker](../src/maintainer_lens/worker.py) | `build_investigation_message_handler()` invokes the injected investigator with `(request, claim_token)` inside the existing runner. The default investigator calls only `investigate_issue()`. `AsyncExitStack` owns process resources. |
| [Message](../src/maintainer_lens/messaging/schemas.py) and [task](../src/maintainer_lens/db/models/investigation.py) | Both contain task/repository/Issue identity. Neither persists a requested language or Source index. The new processor checks identity; the default snapshot-only Worker still has not switched to that processor. |
| [Runner](../src/maintainer_lens/investigation_runner.py) | Commits the claim, runs the processor alongside lease renewal, then commits success or an existing failure/retry transition. The processor returns `None`; successful return permits task completion. |
| [Issue service](../src/maintainer_lens/services/issue_investigator.py) | Fetches a public Issue, preserves GitHub-specific rate-limit/error handling, and returns `IssueSnapshotData` only after its fenced snapshot transaction commits and Session closes. The default Worker may ignore that return value. |
| [Business processor](../src/maintainer_lens/services/investigation_processor.py) | `process_graph_investigation()` reads active-claim identity, reuses the Issue service, invokes Task 63 and commits the guarded result before returning `None`. Dependencies are injected; default Worker is not yet its caller. |
| [Snapshot writer](../src/maintainer_lens/db/issue_snapshots.py) | Accepts a caller-owned Session; checks running status, token and database-time lease, locks the task and upserts by task ID. The caller owns commit/rollback. |
| [Result writer](../src/maintainer_lens/db/investigation_results.py) | Snapshots/revalidates result structure, locks the task and then checks the current database-time lease and identity before upserting. Caller owns commit/rollback; no task status transition. Developer confirmed table creation and persistence tests; new processor now calls it. |
| [Application boundary](../src/maintainer_lens/services/investigation_execution.py) | Accepts validated Issue context and injected dependencies; returns `InvestigationResult`. Owns the Graph deadline and validation, without clients, transactions, leases or result persistence. |
| [Detail query](../src/maintainer_lens/db/investigation_queries.py) and [HTTP route](../src/maintainer_lens/api/routes/investigations.py) | Return task status and optional Issue snapshot. Default Worker has not switched processors; there is no result readback. |
| [Consumer](../src/maintainer_lens/messaging/consumer.py) | ACKs only after the awaited handler returns. Retry/dead forwarding must already have publisher confirmation. |

The investigator callback requires two arguments at runtime.
`run_investigation_consumer()` currently annotates it with one argument;
correct that annotation when Worker wiring is implemented, while preserving
the existing two-argument callback and override seam.

## 2. Shared schema prerequisite

Implemented: [shared contracts](../src/maintainer_lens/schemas/investigation_contracts.py)
now own `CreateInvestigationRequest`, `InvestigationInput` and
`InvestigationResult`, preserving their definitions and inheritance behavior.
The API module explicitly re-exports `CreateInvestigationRequest`; the execution
service explicitly re-exports `InvestigationInput` and `InvestigationResult`.
Old import paths refer to the same class objects, without duplicate validators
or wrapper subclasses.

Before extraction, adding an API response import of the application result
would have produced this cycle:

```text
schemas/investigation_api → services/investigation_execution
                         → schemas/investigation_api
```

The shared module imports existing evidence and analysis types, without
importing the API module, execution service or graph composer. Execution helpers
and deadline ownership stay in the service; the shared invalid-result exception
stays in the existing State module. HTTP response models stay in the API module.

[Contract regression tests](../tests/test_investigation_contracts.py) check
legacy aliases, existing coercion versus strict input behavior, UUID/title/
frozen semantics, result JSON serialization and nested response use. Fresh
interpreter tests cover import order and ensure shared imports do not load the
API/execution/composer modules or invoke settings loading. The developer
confirmed these tests and related regressions passed; no aggregate counts/logs
were supplied.

This resolves the concrete future cycle without duplicating repository
validation or creating a second result representation.

## 3. Implemented processor and Runner injection contract

`services/investigation_processor.py:process_graph_investigation()` is now
implemented with explicit client/session factory, task/token/identity,
language, existing searcher/analyzer types, timeout and optional Source/planner/
tool dependencies. It does not claim, renew, release or complete tasks or
implement delivery retries. The existing Worker callback remains
`async (request, claim_token) -> None`; its production assembly is a later step.
The following flow is available when injecting the processor into the existing
Runner; tests supply that composition, while default Worker wiring is pending.

```text
Existing Runner: commit claim; start processor + heartbeat
  Processor:
    short Session/transaction: read and validate claimed task identity
    await existing Issue service: public GitHub read + fenced snapshot commit
    construct InvestigationInput from validated identity and returned snapshot
    await execute_graph_investigation(...): complete Graph deadline
    short Session/transaction: fenced Graph result upsert; commit
    return None only after successful result commit
Existing Runner: stop heartbeat; guarded task-success transaction
Existing Handler/Consumer: return after processing/confirmed forwarding; ACK
```

The processor first validates repository/Issue using the shared submission
model with strict validation. In a short transaction,
`db/investigation_tasks.py:get_claimed_investigation_identity()` reads only
repository/Issue for a running task with the supplied token and a lease valid
at PostgreSQL `clock_timestamp()`. It returns a plain tuple or `None`, without
locking the row or committing. The processor maps `None` to `LeaseLostError`
and mismatched request identity to `PermanentInvestigationError` before any
GitHub/retrieval/model call, then uses database identity for subsequent work.
The read Session closes before network work. This preflight does not authorize
later writes indefinitely; each existing writer fences independently.

`investigate_issue()` retains its existing GitHub fetch/error classifier and
now returns the existing immutable `IssueSnapshotData` after snapshot commit
and Session cleanup. Existing snapshot-only callers ignore the value. This
avoids a second GitHub read and a DB reread solely to recover title/body; no
live ORM object or Session is passed to the Graph.

Pass title/body from that committed snapshot, validated repository/Issue,
explicit response language and any approved Source index into
`InvestigationInput`. The processor supplies injected searchers/analyzer and
execution budgets using the existing Task 63 signature. Full Graph input and
options validation remains in that application entry after Snapshot preparation;
do not claim that an invalid Graph option prevents the preparatory GitHub read.
The caller supplies a maximum-round budget, not an internal current-round
counter; callers cannot inject State findings, evidence or route values.
The processor commits the result in its own short transaction, maps a `False`
writer return to `LeaseLostError`, and
returns `None` only after that commit/Session cleanup. No blanket exception
wrapping is added; original GitHub errors, Graph failures and cancellation
propagate to the caller's existing policy.

## 4. Knowledge and resource policy

**Recommended first wiring profile, subject to design approval:** use existing
History knowledge, one search round, an explicit backend-selected response
language and positive finite Graph timeout. Defer the actual language and
numeric timeout choice to the wiring slice; no new settings are added here.
Query Planner and Tool Runner remain supported by the independent application
entry point but need not be enabled in the first production connection.

An empty History result is legitimate evidence insufficiency. Do not silently
start ingestion, refresh embeddings or convert an empty corpus into indefinite
delivery retries. Knowledge preparation remains an explicit separate operation.

Source remains optional. To enable it in production, a separate preparation
slice must supply a trusted, explicit ready index UUID and validate repository,
provider/model and dimensions. Never select an arbitrary "latest" index, let
the model choose an index, or silently disable requested Source search. A ready
index with zero matching evidence is a normal investigation result. Missing,
failed or incompatible requested indexes must be distinguished from a genuinely
in-progress build when defining bounded failure/defer policy. The existing
private `_find_existing_index()` is a build helper, not an accepted general
production index-selection API.

Per-task language/index choices would require explicit submission, task and
message contracts; these values are absent today. Do not add them incidentally
to persistence or assume the broker already carries them. The initial
History-only profile is an intermediate integration, not completion of the
eventual Source+History production E2E milestone.

The Worker's `AsyncExitStack` will own shared GitHub/LLM/Embedding HTTP clients
and engine disposal. Reuse existing `llm_*` and `embedding_*` settings and
provider factories. Validate required provider configuration before consuming
Graph work; injected investigator tests should not require real credentials.
Register cleanup as each resource is acquired, including partial startup.
Each retrieval/heartbeat/write operation keeps its own short Session; no
Session or transaction spans a GitHub, Embedding, LLM or broker request.

Task 63's timeout covers Graph invocation only. It does not bound GitHub
snapshot acquisition, knowledge preparation, result commit or the complete
delivery lifecycle. Retain appropriate existing transport/database bounds;
do not describe the Graph budget as an entire-job deadline or cost limit.

## 5. Durable result contract

Implemented in the [ORM](../src/maintainer_lens/db/models/investigation_result.py)
and registered through `db/models/__init__.py`: one `investigation_results` row
per task, represented by `InvestigationResultRecord` to distinguish it from the
Pydantic `InvestigationResult`. The [Alembic revision](../migrations/versions/c4b2a6d8e901_create_investigation_results.py)
extends `789b1caf1332` with revision `c4b2a6d8e901`. The developer reports the
table was created and tests passed; the assistant did not apply the migration
or query the live DB revision.

| Column | Responsibility |
| --- | --- |
| `task_id` | Primary key and cascading foreign key to the existing task; one canonical current result |
| `schema_version` | Version of the stored result representation; initially 1 |
| `result_payload` | JSONB containing the complete validated `InvestigationResult.model_dump(mode="json")` |
| `created_at`, `updated_at` | Database-generated persistence timestamps; preserve creation time on upsert |

Do not add a competing task status or a table of speculative execution stages.
The payload retains request context, structured analysis, complete returned
History/Source evidence, actual `analysis_evidence`, next step, rounds and
routing reason. Do not store credentials, claim tokens, raw internal Graph
State, prompts or free-text tool feedback. Do not depend on live knowledge rows
to reconstruct old analysis excerpts, or truncate evidence during persistence.
The result is a stored preliminary investigation, not an implemented final
report-writing node or proof of semantic correctness.

The [DB helper](../src/maintainer_lens/db/investigation_results.py)
`upsert_investigation_result(session, *, task_id, claim_token, result) -> bool`
receives the validated shared result model. Before its first await it checks
the model/extra fields, revalidates plain structural data (including mutable
nested models), snapshots the JSON payload and rejects non-finite/non-JSON
values. Citation/routing validation stays in the application boundary. Its
caller owns the transaction; `True` means SQL execution, not committed durability.

In that same short transaction, first lock the task by ID with `FOR UPDATE`.
After the lock is acquired, use a separate statement to check running status,
token and PostgreSQL `clock_timestamp()` lease validity. This prevents lock-wait
time from preserving a pre-expiry check. Compare payload repository/Issue with
the locked task. Wrong identity raises `InvalidInvestigationResultError`;
missing/invalid ownership returns `False`, which the processor maps to
`LeaseLostError`. Only then upsert by task ID, replacing the payload/version
and updating `updated_at` while preserving `created_at`. SQL failures and
cancellation propagate. Do not infer task completion from this artifact.

The migration and ORM enforce a positive schema version and object-shaped
JSONB. [Complete tests](../tests/integration/test_investigation_result_persistence.py)
include default offline metadata/serialization/error checks and opt-in real
PostgreSQL tests for schema, payload preservation, transactions, claim fencing,
lock waits, expiry and cancellation. No test runs migrations or calls providers.
The developer confirmed table creation, persistence tests and subsequent
processor tests passed; HTTP readback tests still await execution.

Repeated writes from the valid owner replace one row; a stale owner cannot
overwrite a replacement owner's data. Fencing protects artifacts, not
exactly-once external computation.

## 6. Completion, crash windows and readback

Keep result commit and the existing Runner's success commit separate, as with
Snapshots. Making them atomic would require a larger Runner finalization
contract change; it is not part of this proposal.

| Failure point | Required outcome |
| --- | --- |
| After snapshot commit, before result commit | Snapshot may exist; do not mark task successful or treat the snapshot as a Graph result |
| Before/during result write | Roll back the failed write; propagate the error or lost ownership; no successful processor return |
| After result commit, before task-success commit | A result may exist while the task remains running/pending; its existence alone is not completion |
| New owner claims after failure/lease expiry | Reexecute through the existing processor and fenced-upsert the result; do not treat an old row as a checkpoint/cache |
| After success commit, before ACK | Redelivery observes existing succeeded status through the current handler path; do not recompute solely because ACK was lost |

The implemented HTTP detail response adds an optional typed `result` alongside
the existing Issue snapshot. Read task/result consistently and expose a result
as completed output only when task status is `succeeded`. For pending/running/
failed tasks return `result=None`, even if an earlier attempt committed a row.
Schema-version or payload corruption must remain visible as an integrity error,
not fabricated success or silent evidence loss.

The [detail query](../src/maintainer_lens/db/investigation_queries.py)
`get_investigation_details()` uses one SELECT with two left outer joins to read
task, optional Snapshot and optional result. It refreshes cached ORM objects
using `populate_existing` so reused Sessions do not retain an older status or
payload. No row locks, writes, provider calls or additional status query are
introduced. The request-scoped Session owns cleanup of the read transaction.

Only after the succeeded check, the [route](../src/maintainer_lens/api/routes/investigations.py)
calls `decode_investigation_result()` in the existing
[result helper](../src/maintainer_lens/db/investigation_results.py). It requires
schema version 1, checks record/task ID, rejects non-JSON/non-finite data, strictly
decodes the shared result model from JSON (including serialized UUIDs), and
compares repository/Issue identity to the task. This is storage structure and
identity validation; it does not rebuild model context, rerun Graph provenance
or routing checks, or promise semantic fact verification. The stored original
analysis excerpts and complete evidence are returned without truncation.

`StoredInvestigationResultError` becomes HTTP 500 with the fixed detail
`Stored investigation result is unavailable`; payloads, validation errors and
exception messages are not copied into the response. DB errors and external
cancellation propagate without blanket wrapping. The
[complete readback tests](../tests/integration/test_investigation_result_readback.py)
cover offline FastAPI/decoder/query behavior and opt-in PostgreSQL joins,
invalid artifacts, null legacy results and committed status/payload visibility.
Existing [API tests](../tests/api/test_investigations.py) retain POST/404/Snapshot
coverage and now expect the additional nullable result field.

Legacy snapshot-only `succeeded` tasks legitimately have no result row; return
`result=None` for them. Do not backfill them by calling providers or reinterpret
them as failed. New Graph processors must enforce result-before-return, while
the generic root Runner continues to support its existing injected processors.

Both `draft_preliminary_report` and `needs_more_evidence` are successful Graph
outcomes when durably saved. `succeeded` means execution and required writes
completed; it does not mean the investigation proved a root cause.

## 7. Error and cancellation mapping

| Event | Contract with the existing backend |
| --- | --- |
| Invalid input/result/citations or message/task identity mismatch | Permanent failure through existing typed errors/`ValueError` classification |
| Graph-owned deadline or upstream timeout/network/transient HTTP failure | Existing bounded retry policy; final attempt becomes failed/dead |
| GitHub rate limit | Preserve the existing typed error and `not_before` deadline |
| Valid insufficient-evidence result | Persist and allow success; no automatic failure/retry |
| `LeaseLostError`, including pure lease-loss groups | Preserve the signal; current handler rereads authoritative status; stale owner performs no replacement-claim update |
| Mixed lease/other groups or heartbeat failures | Preserve existing propagation; do not flatten/reclassify them in the new processor |
| External `CancelledError` | Propagate after cleanup; no deliberate success or claim release |
| Result-transaction error / unknown processor failure | Propagate to existing processor classification; no new retry loop |
| Runner final-status commit or confirmed-forwarding failure | Existing infrastructure path stays visible; do not ACK or claim success |

The [complete processor test file](../tests/integration/test_investigation_processor_pipeline.py)
supplies offline real-Graph orchestration tests and opt-in real PostgreSQL/
Runner tests for completed/insufficient/empty results, identity rejection,
deadline/citation/write/commit failures, replacement claims and cancellation.
GitHub uses MockTransport; searchers/analyzers are fake. One heartbeat-failure
case injects a failing heartbeat to exercise TaskGroup cancellation. No broker,
paid API or migration is run. These tests have not been run by the assistant
or confirmed by the developer yet.

## 8. Small implementation slices and acceptance

Implement and explain one slice per round:

1. **Shared schemas.** Add `schemas/investigation_contracts.py`, preserve old
   imports/validation, and verify Agent/application and HTTP-schema regressions.
   Developer confirmed the contract and related regressions passed. No Worker
   wiring or external calls were introduced by this slice.
2. **Fenced result storage.** Add the result ORM, one reviewed Alembic migration
   and DB helper. Put the ORM model in a dedicated file under the existing
   `src/maintainer_lens/db/models/` directory and register it through
   `db/models/__init__.py` (imports and `__all__`). Inspect migration heads before
   creating the revision file. The developer will manually execute Alembic
   upgrade; the assistant must not apply it. Shared Pydantic contracts do not
   create tables and need no ORM registration or migration themselves.
   Test JSON preservation/versioning, idempotency, identity rejection, rollback,
   expired/replaced claims and task-row locking with isolated real PostgreSQL.
   Never use only Session mocks to claim concurrency/fencing correctness.
   ORM registration, helper, revision `c4b2a6d8e901` and complete tests are now
   supplied; the developer confirmed table creation and tests passed. The new
   processor calls the writer, while default Worker still has not switched.
3. **Business processor.** Extend the Issue service's committed-data return and
   add the identity → snapshot → Graph → result collaborator with injected
   dependencies. Verify both terminal outcomes, no-network identity rejection,
   cancellation/lease loss, result-commit failure and no success before commit.
   Use real compiled Graph and Runner, mocked providers; opt-in DB where needed.
   Implemented in the working tree; developer reports pipeline tests passed.
   Worker, Runner, HTTP, Settings and migrations were not
   changed by this slice.
4. **HTTP readback.** Extend the existing query/response without changing intake.
   Test completed results, hidden partial attempts, legacy null results, 404
   and explicit corrupt/unsupported stored representations.
   Implemented with complete tests awaiting developer execution. No migration
   or default Worker change is required by this slice.
5. **Worker assembly.** Approve concrete language/budget/provider profile, bind
   existing adapters/analyzer in `AsyncExitStack`, switch the default callback
   only after persistence/readback exist, fix its callback annotation and test
   injected overrides, startup failures, closure and ACK/confirmed retry ordering.
6. **End-to-end verification.** First use fake providers with real infrastructure
   under explicit opt-in; live public GitHub/LLM/Embedding verification requires
   separate cost/side-effect authorization. Source preparation/selection must
   be designed and verified before claiming the full Source+History Worker E2E.

No slice implies automatic dependency installation, DB migration execution,
paid calls, Git operations, checkpointing, HITL, a new queue/status engine or a
cost system. A changed default Worker remains a separately reviewable step.
