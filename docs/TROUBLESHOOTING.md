# Investigation troubleshooting

PostgreSQL owns task status, safe events and saved artifacts. Worker console logs
add bounded diagnostic detail; they are not a second task state machine. This
guide describes the local Compose deployment, without a centralized log service,
distributed tracing or a demonstrated availability SLA.

The [Web interface](WEB_INTERFACE.md) presents safe task/event records at `/logs`.
For startup, migration and connection settings, use [installation](INSTALLATION.md)
and [configuration](CONFIGURATION.md).

## Locate an investigation by Issue URL

Keep API, Dispatcher and Worker running when inspecting a deployed investigation.
Use the read-only lookup instead of POST when you only want to inspect history:

```bash
curl --get 'http://127.0.0.1:8000/investigations/lookup' \
  --data-urlencode 'issue_url=https://github.com/pypa/sampleproject/issues/200' \
  --data-urlencode 'limit=20'
```

The response contains `repository`, `issue_number`, `tasks` and `next_before`.
Each task locator includes `task_id`, `status`, `generation`, `created_at` and
`updated_at`. All submission modes and preparation profiles for that repository
and Issue are included, newest first by `(created_at, task_id)`. Repository
matching is case-insensitive. Unknown Issues return an empty list. A successful
task may legitimately lack a report if it predates Graph integration.

For an older page, send the same URL with `before=<next_before>`. `limit` is
1–100, default 20. A missing/deleted or other-Issue cursor returns HTTP 400;
restart from the first page if the cursor was deleted. Newer insertions do not
shift following pages, but pagination is not a database snapshot and task status
can change between reads. Returned-row limits are not a database query-time SLA.

Lookup executes only SELECTs, without row locks. It does not contact GitHub,
load model credentials, check freshness, acquire claims, create Outbox events,
resume a failure or approve human review. It also finds old Tasks no longer
anchored by URL reuse. It is a task locator, not a report freshness certification.
The existing local API has no user isolation; this endpoint does not make the
deployment suitable for public exposure.

## Read the status, failure and committed phases

Copy a `task_id` from lookup, then use the existing read endpoints:

```bash
INVESTIGATION_TASK_ID='paste-task-id-from-lookup'
curl "http://127.0.0.1:8000/investigations/$INVESTIGATION_TASK_ID"
curl --get "http://127.0.0.1:8000/investigations/$INVESTIGATION_TASK_ID/events" \
  --data-urlencode 'after=0' --data-urlencode 'limit=100'
```

Details expose `progress`, `last_error`, `generation`, freshness and available
reports. Events expose committed lifecycle/progress records; continue with
`after=<next_after>` when present. These endpoints do not run a new investigation.
`last_error` can be historical even when a later attempt succeeds. Always compare
the event generation and status before attributing an error to current work.

- `needs_more_evidence` is a valid succeeded investigation with explicit gaps.
- `waiting_review` needs one of the existing explicit review decisions; repeating
  a URL does not approve it.
- `retry_scheduled` uses the existing finite MQ retry policy. A stopped process
  may leave the last committed phase until lease/redelivery recovery.
- GitHub closed state is displayed separately from evidence of a code repair.
- POST `/investigations/from-url` can create, check or recover work and can incur
  provider charges. Do not use POST as a read-only log search.

## Correlate Worker diagnostics

The standard Worker emits JSON messages through Python logging. Its console
prefix still contains the existing timestamp/level/logger formatting. Match
the `task_id` rather than treating every HTTP 200 as a successful analysis:

```bash
docker compose logs --no-color --since=2h worker | rg --fixed-strings "$INVESTIGATION_TASK_ID"
```

Messages include task ID, repository, Issue number, delivery generation, event,
timestamp, last committed phase and its search round where available. Direct
Runner calls outside the Worker context still include task ID/generation but
can have null repository/phase fields. Context is scoped to one delivery and
reset on return, exception or cancellation; concurrent deliveries do not share
identity. Awaited Graph/TaskGroup descendants share that delivery's last phase.
This is a last-reported phase, not a full distributed span or exact attribution
of parallel suboperations.

| Event | Meaning |
| --- | --- |
| `claim_committed` | The claim transaction returned successfully |
| `phase_committed` | The progress event transaction returned successfully |
| `processor_failed` | Original processor failure observed before status persistence; inspect retryability and later DB events |
| `execution_interrupted` | Execution/lease maintenance raised, including external cancellation; the original exception is re-raised |
| `failure_committed` / `retry_committed` | The existing fenced status transaction returned successfully |
| `review_committed` / `completion_committed` | The existing review/success transaction returned successfully |

Failure diagnostics contain exception types and bounded code locations
(`file`, `function`, `line`), including bounded group/cause structure. They do not
format the original exception, collect locals, or include raw HTTP bodies,
model output, claim tokens, prompts or API keys. Pydantic details retain
allowlisted analysis field paths and error types, for example:

```json
{"location": ["findings", 0, "evidence_ids"], "type": "too_short"}
```

Unknown field names are replaced with `<field>` so dynamic input keys are not
logged. Other error text remains in the existing safe API error codes. An empty
`findings` list can be valid; an individual finding with no reference is invalid.
Logging itself does not repair model output or trigger provider calls; the
analysis adapter's one schema-correction allowance is a separate execution rule.
Ordinary diagnostic formatting/sink errors are contained in the logging helper
so they cannot replace a business error or change task status. No raw-text
fallback is emitted.

A `processor_failed` log can exist even if a later status commit fails. Logs can
also be missing after abrupt termination or storage failure. Use PostgreSQL
status/events as authority; absence of a completion log does not prove failure.
Existing third-party/access logs are not globally reformatted or sanitized by
this helper. Do not put credentials into request URLs or log additional bodies.

## Retention and exporting before container replacement

Compose explicitly uses Docker's `json-file` driver for API, Dispatcher, Worker
and the migration service, with `max-size: "10m"` and `max-file: "3"` per
container. Logs rotate by size, not days; this is a bounded local diagnostic
window, not a backed-up archive or retention SLA. Infrastructure service logging
continues to follow its existing Docker configuration. See Docker's
[driver options](https://docs.docker.com/engine/logging/drivers/json-file/) and
[Compose logging configuration](https://docs.docker.com/reference/compose-file/services/#logging).

Docker owns the log files; inspect/export them through Docker commands. A restart
of the same container can retain its remaining logs. Container removal/recreation
does not preserve a project-managed archive of the old logs. Before replacing
containers, export useful diagnostics to ignored local storage:

```bash
mkdir -p .local/diagnostics
docker compose logs --no-color --timestamps --since=2h api dispatcher worker \
  > .local/diagnostics/application-before-rebuild.log
```

Use another filename when keeping multiple exports. Restrict access and inspect
the export before sharing it. DB events/checkpoints follow their existing
persistent storage and deletion rules; this change adds no cleanup/TTL or backup.

Apply changed code or container logging options by rebuilding/recreating the
applications; a plain restart does not install them. For schema changes, follow
the migration step in the [upgrade procedure](INSTALLATION.md#upgrading-an-existing-installation).

```bash
docker compose stop api dispatcher worker
docker compose build api
docker compose up -d --no-deps --force-recreate api dispatcher worker
docker compose config -q
```

These are maintainer commands, not automatically executed by tests. They do not
delete PostgreSQL volumes. No new paid investigation is needed just to inspect
lookup, details, events or retained logs.

## Focused acceptance

Offline tests use mocked I/O and do not load real model credentials:

```bash
uv run --locked pytest -q \
  tests/test_investigation_diagnostic_logging.py \
  tests/api/test_investigation_lookup_routes.py \
  tests/test_investigation_runner_retry.py \
  tests/test_investigation_progress_contracts.py \
  tests/test_worker_production_wiring.py \
  tests/api/test_investigations.py
```

The isolated PostgreSQL lookup test creates unique Task/Event fixtures and
deletes only those records. It creates no Outbox messages and calls no providers;
services can stay running for this file alone. Existing migrations must already
be applied, and the existing DB configuration must point to your test database:

```bash
RUN_POSTGRES_INTEGRATION=1 uv run --locked pytest -q \
  tests/integration/test_investigation_lookup_postgres.py
```

Broader existing PG/MQ suites retain their documented isolation requirements;
the permission to keep services running above applies only to this lookup file.
For full-suite isolation and opt-in paid acceptance, see [development and tests](DEVELOPMENT.md).
