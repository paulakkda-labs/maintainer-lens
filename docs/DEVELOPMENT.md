# Development, tests and contributing

[Documentation](README.md) · [Architecture](ARCHITECTURE.md) · [Installation](INSTALLATION.md)

## Local tooling

Python development needs Python 3.12+ and uv. Frontend development needs Node.js
22.12+. Docker users do not need either installed on the host.

```bash
uv sync --locked --dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q
```

The default tests use mocks/fixtures and skip explicitly opt-in infrastructure
and paid acceptance paths. Leave `RUN_LIVE_*` and `RUN_DEPLOYED_REPORT_QUALITY`
unset unless you intend to run the corresponding external tests. Never use real
provider keys as test constants or include `.env` in a report.

For backend processes on the host, follow [host-mode setup](INSTALLATION.md#optional-python-services-on-the-host).
Settings are loaded from the repository-root `.env`; the shell environment can
override values. Container settings instead come from Compose. Avoid accidentally
running a host Worker and Docker Worker against the same work queue while debugging.

## Frontend development

Keep a backend at `http://127.0.0.1:8000` and run Vite in another terminal:

```bash
cd web
npm ci --ignore-scripts --no-audit --no-fund
npm run dev
```

Vite proxies investigation/health/API-documentation requests to port 8000;
adjust `web/vite.config.ts` if using a different development backend. This proxy
is separate from the Docker same-origin production-style asset serving.

Offline frontend checks:

```bash
cd web
npm run format:check
npm run build
npm exec -- playwright install chromium
npm test
```

`build` includes TypeScript checking. Browser tests intercept investigation APIs;
they do not validate live GitHub, providers or the deployed Worker. Do not commit
browser reports, screenshots from test runs or `node_modules`.

## Infrastructure integration

Use a **disposable test database and RabbitMQ vhost**, apply current migrations,
and stop application processes that share those test resources. Outbox/queue
fixtures can interact with a running Dispatcher/Worker even when test rows use
unique IDs. Do not point the integration suite at a database containing reports
you need to keep.

With the test `DATABASE_URL` and `RABBITMQ_URL` configured in your test environment:

```bash
uv run --locked alembic upgrade head
RUN_POSTGRES_INTEGRATION=1 \
RUN_RABBITMQ_INTEGRATION=1 \
RUN_WORKER_E2E=1 \
uv run --locked pytest -q
```

These switches enable real infrastructure; they do not themselves opt into
paid provider tests. Provider requests in ordinary integration fixtures remain
simulated. Individual paid tests require additional explicit switches described
by their test files. Keep those switches unset for ordinary regression.

## Deployed URL acceptance

This is a different test mode: keep API, Dispatcher and Worker **running** in a
dedicated local deployment. The HTTP-only check submits a real public Issue,
polls the result/events and checks same-Task report reuse. It may download data,
call paid providers and leave durable tasks/knowledge.

```bash
RUN_DEPLOYED_REPORT_QUALITY=1 \
REPORT_QUALITY_BASE_URL=http://127.0.0.1:8000 \
REPORT_QUALITY_CASES=license-metadata \
uv run --locked pytest -q -s \
  tests/integration/test_deployed_investigation_report_quality.py::test_deployed_url_to_report_quality
```

The original small-repository case uses Small. Use your actual dedicated API port;
only loopback origins are accepted. Default selection is one case, so other cases
skip. Artifacts are local and include a pending human-review template.
[Evaluation and additional cases](REPORT_QUALITY.md)

For a cold test, start with a dedicated empty database/broker volume set; retain
proof that knowledge/results were absent before submission. Reusing a cached
result is a useful readback test, but does not demonstrate cold preparation.
Clean up only the disposable environment after saving needed artifacts.

## CI and packaging

The [CI workflow](../.github/workflows/ci.yml) has three jobs:

1. Python lint/format, default pytest and wheel/source-distribution build.
2. Frontend formatting, type-check/build and offline browser tests.
3. Disposable PostgreSQL/pgvector and RabbitMQ, Alembic, FastAPI startup and
   infrastructure integration tests with fake providers.

CI success is not a paid-provider or semantic accuracy certification. Locally,
`uv build` builds Python distributions; Web assets remain a separate build that
Docker copies into the application image.

## Contributing

Use a focused branch and pull request. Explain the user-visible problem,
implemented behavior and checks actually run; distinguish mocks, real infrastructure
and paid/deployed tests. Preserve unrelated changes and avoid reporting skipped
checks as passed.

For workflow changes, preserve transaction ownership, at-least-once delivery,
ACK-after-handler completion, claim/lease fencing, independent Sessions and
cancellation propagation. Add risk-based regression coverage for changed boundaries.
See the [module navigation map](ARCHITECTURE.md#module-navigation-map).

Keep public docs current when changing interfaces or operations. New tables go
under `db/models`, are registered in its `__init__.py`, and use Alembic migrations.
Architectural tradeoffs belong in [ADRs](ADR/README.md). Never commit secrets,
local progress diaries, generated acceptance artifacts or real `.env` files.
