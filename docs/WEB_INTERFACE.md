# Web interface

V1 has two same-origin pages: `/` for public GitHub Issue investigation and
`/logs` for read-only task/event lookup. Swagger remains at `/docs`. React,
TypeScript and Vite build static assets served by the existing FastAPI process.
Docker uses Node only during the build; no extra frontend runtime service or
provider credentials in the browser are required.

## Upgrade and run

Alembic revision `3cd7a9b1e528` follows `1bc6f8a0d417`. It adds
`investigation_tasks.response_language` with an `en/zh` constraint. Existing
production tasks retain Chinese; new database INSERTs and public submissions
default to English. Report/Run/checkpoint JSON and historical Chinese URL reuse
hashes are preserved. Downgrade refuses while non-Chinese tasks exist.

Stop old applications, apply the migration manually, and run the tests below.
PostgreSQL, Redis and RabbitMQ remain available:

```bash
docker compose stop api dispatcher worker
uv run --locked alembic upgrade head

# After test acceptance; export any useful old logs before recreation.
docker compose build api
docker compose up -d --no-deps --force-recreate api dispatcher worker
```

Services share the application image. A restart alone does not install this
code/protocol. Open [investigation](http://127.0.0.1:8000/) or
[execution records](http://127.0.0.1:8000/logs). No `.env` change or manual Source
UUID is needed. Start with a small repository and explicitly select Small in
Preparation budget. A new investigation can incur embedding/model charges;
read-only record lookup does not.

## Language, progress and reports

- The first visit is English. The top-right switch locally translates labels,
  recorded phases and safe errors, remembering the browser preference.
- Submission snapshots `response_language: "en"` or `"zh"` through Task,
  Outbox, Dispatcher, MQ, Worker, frozen execution and review recovery. Server
  technical diagnostics stay English; original evidence/code is preserved.
- Changing UI language does not mutate a task, translate its saved report or
  automatically call the model. An explicit alternate-language report action
  submits/resolves the selected language's investigation.
- Languages have separate task/reuse identities but share compatible prepared
  Source/History knowledge. Checkpoint recovery keeps the original language.
- Old queue messages which omit language retain the legacy Chinese meaning.
  New English messages include it. API clients needing Chinese should explicitly
  submit the field now that the public default is English:

```json
{
  "issue_url": "https://github.com/pypa/sampleproject/issues/225",
  "response_language": "zh",
  "knowledge_budget_profile": "small"
}
```

The workspace shows Task ID, status, generation and report language. Standard
remains the preparation default; Unlimited only releases preparation task
limits while retaining existing Graph controls. Preparation may reuse an index.

Detail/events are polled approximately every two seconds while active, with
incremental event IDs. These are recorded phases, not an estimated percentage
or a separate completion event for each HTTP call. Failed reads retry GETs and
show uncertainty; they neither mark a task failed nor repeat POSTs. Navigating
away does not cancel server work. Existing claims/leases/retries remain owners.

Reports default to preview with tables, code and evidence links; raw Markdown,
copy and `.md` download are available. `react-markdown` rendering excludes raw
HTML, remote images and unsafe link protocols. The Web shell has a same-origin
content policy. These protections do not certify the report's claims.

Long reports scroll inside a bounded report pane in both preview and raw modes.
The height limit follows the viewport height with a small extension below it,
rather than a fixed pixel cap, so larger windows have more room for reading.
Wheel gestures outside the pane scroll the page. Inside the pane, reaching the
top or bottom does not pass scrolling to the page. The pane can receive keyboard
focus for scrolling; short reports retain their natural height.

`needs_more_evidence` is a valid completed report. An earlier report displayed
while new work is pending identifies its owning Task. Freshness describes the
existing scoped metadata check, not exhaustive PR/discussion coverage.

Waiting reviews expose backend-eligible budget increases, acceptance of an
available validated stage analysis, or rejection. Approval is explicit; retries
of the same action/budget reuse the decision's idempotency ID.
Stage acceptance first exposes the complete saved analysis and actual evidence
context so the operator can inspect the artifact before deciding.

## Records and retained state

Records accepts a Task ID or Issue URL. URL search uses GET lookup; task selection
uses existing detail/events GETs. It displays safe persisted events/errors, not
Docker files. See [developer troubleshooting](TROUBLESHOOTING.md) for technical logs.

The two workspaces retain independent state across navigation and back/forward.
Refresh restores Task references and reads existing reports via GET without
submission. Session storage holds URL/IDs/profile; local storage holds language.
PostgreSQL remains authoritative for reports. Disabled storage retains only the
current in-memory workspace; closed/cleared sessions have no guaranteed history.
There is no account history, authentication or user isolation. This remains a
localhost development deployment rather than a hardened public hosting service.

## Development and acceptance

Docker users need no host Node. Source development uses Node 22.12+:

```bash
cd web
npm ci --ignore-scripts --no-audit --no-fund
npm run dev
```

Vite proxies APIs to `http://127.0.0.1:8000`. For a source Python deployment, first
run `npm run build`, return to the repository root, then start FastAPI using
existing configuration. FastAPI serves `WORKDIR/web/dist`; start it after
building. Python wheels alone do not bundle this separate frontend build.
Without built assets, Web entries return 503 while API/Swagger remain available.

Offline browser tests intercept investigation APIs, without real providers,
GitHub, databases or RabbitMQ:

```bash
cd web
npm run format:check
npm run build
PLAYWRIGHT_BROWSERS_PATH=../.local/playwright-browsers npm exec -- playwright install chromium
PLAYWRIGHT_BROWSERS_PATH=../.local/playwright-browsers npm test
```

They cover languages, live phases/errors, preview/download safety, retained
navigation/refresh, lookup and explicit idempotent review. Mock screenshots are
saved under ignored `.local/web-review`; they are not real-provider acceptance.
CI adds a separate locked Web build/browser job.

The maintainer runs pytest. Focused offline regression:

```bash
uv run --locked pytest -q \
  tests/test_investigation_web_contracts.py \
  tests/test_investigation_contracts.py \
  tests/api/test_investigations.py \
  tests/test_investigation_progress_contracts.py \
  tests/test_investigation_url_resolution.py \
  tests/test_investigation_human_review.py \
  tests/test_worker_production_wiring.py \
  tests/messaging/test_publisher.py \
  tests/integration/test_investigation_processor_pipeline.py \
  tests/integration/test_investigation_result_persistence.py \
  tests/integration/test_investigation_result_readback.py
```

Keep application processes stopped for the existing integration suites, which
include transactional Outbox fixtures. Existing migrations must be applied:

```bash
RUN_POSTGRES_INTEGRATION=1 uv run --locked pytest -q \
  tests/integration/test_investigation_url_reuse_postgres.py \
  tests/integration/test_investigation_result_persistence.py \
  tests/integration/test_investigation_result_readback.py \
  tests/integration/test_investigation_processor_pipeline.py \
  tests/integration/test_investigation_human_review_postgres.py
```

Before merging, run default full regression and observe GitHub CI. New offline
Web tests require no paid calls. Manual deployed Issue trials are a separate,
potentially paid acceptance step.
