# MaintainerLens

**Evidence-driven GitHub issue investigation on a reliable asynchronous Python backend.**

MaintainerLens is an Apache-2.0-licensed project under active development. Its
current working vertical slice accepts a **public GitHub issue**, records an
investigation request transactionally, dispatches it through RabbitMQ, fetches
the issue in a background worker, and exposes the persisted snapshot through an
HTTP API.

Repository indexing, retrieval-augmented generation (RAG), multi-agent analysis,
and evidence-backed investigation reports are **planned, not yet implemented**.
See the [roadmap](docs/ROADMAP.md) for milestone status and future scope.

## Current architecture

```text
Client
  |
  | POST /investigations
  v
FastAPI
  |
  | One PostgreSQL transaction
  v
InvestigationTask + InvestigationOutboxEvent
  |
  v
Outbox Dispatcher -- publisher confirms --> RabbitMQ
                                            |
                                            v
                                      Worker + Runner
                                      (claim token + lease)
                                            |
                                            v
                                      GitHub REST API
                                            |
                                            v
                              PostgreSQL: Issue Snapshot + Status
                                            |
                                            v
                          GET /investigations/{task_id}
```

The API, dispatcher, and worker are **independent, long-running services**.
The migration job is a separate **one-off task**, not a seventh long-running
service. Message delivery and execution use **at-least-once semantics**; claim
tokens, leases, and guarded persistence help tolerate duplicate or delayed
processing. Redis is integrated as infrastructure; cross-worker rate limiting
and caching are future work.

## Requirements

**Recommended: Docker workflow**

- Docker Desktop or Docker Engine with the Docker Compose v2 plugin.
- Network access to the image registries used by `compose.yaml` and `Dockerfile`.
- Git to clone the repository.

**Optional: run Python services on the host**

- Python **3.12+**, [uv](https://docs.astral.sh/uv/), and Git.
- PostgreSQL 17 + pgvector, Redis 7.4, and RabbitMQ 4.2, conveniently started
  using the same Compose project.

## Initial setup

```bash
# Clone the repository, then work from its root.
cp .env.example .env
```

Edit `.env` and set the PostgreSQL, Redis, and RabbitMQ credentials and host-mode
connection URLs. **Do not commit `.env`, tokens, or credentials.** If existing
containers use persistent data volumes, retain their existing credentials:
changing `POSTGRES_PASSWORD` or `RABBITMQ_DEFAULT_PASS` in Compose does not
reset users/passwords already stored in those volumes.

**RabbitMQ virtual host:** use the existing, single configuration variable
`RABBITMQ_VHOST`. For the current development environment:

```dotenv
RABBITMQ_VHOST=maintainer_lens
```

The RabbitMQ container's virtual host and the three Python services' AMQP URLs
must refer to **the same virtual host**. The current name `maintainer_lens` is
safe to use directly as a URL path segment. Other virtual-host names containing
reserved URL characters (for example `/`) require URL encoding in AMQP URLs;
this development Compose setup assumes a URL-safe name. The obsolete
`RABBITMQ_VHOST_URLENCODED` variable is **not required** in the aligned Compose
configuration.

When running Python on your Mac, `DATABASE_URL`, `REDIS_URL`, and `RABBITMQ_URL`
in `.env` use `127.0.0.1`. Containerized application services instead receive
Compose-generated URLs using the internal DNS names `postgres`, `redis`, and
`rabbitmq`. Use URI-safe credentials, or ensure reserved characters are encoded
in connection URLs.

## Everyday Docker commands

Run these from the repository root. Compose can be invoked repeatedly: it
reuses unchanged running containers and starts or recreates services as needed.
`--build` rebuilds the application image; it **does not delete named volumes**.

| Task | Command |
| --- | --- |
| **Start/update the entire stack** (six services + migration job) | `docker compose up -d --build` |
| **Start infrastructure only** (PostgreSQL, Redis, RabbitMQ) | `docker compose up -d postgres redis rabbitmq` |
| **Stop application services, keep infrastructure available** | `docker compose stop api dispatcher worker` |
| **Stop all services, preserve containers and data** | `docker compose stop` |
| Restart applications **without** rebuilding | `docker compose restart api dispatcher worker` |
| Apply application code/dependency changes | `docker compose up -d --build` |
| Check status, including the migration job | `docker compose ps -a` |
| Inspect application logs | `docker compose logs -f api dispatcher worker` |
| Inspect all logs | `docker compose logs -f` |
| Check Compose syntax/configuration | `docker compose config -q` |

**Typical day-to-day workflow:** keep PostgreSQL, Redis, and RabbitMQ running
while coding and executing integration tests; stop `api`, `dispatcher`, and
`worker` when you do not need the full investigation pipeline. Run
`docker compose up -d --build` whenever you need the complete application.

`docker compose restart` only restarts existing containers: it **does not**
install modified source or rebuild images. A Docker-based application must be
rebuilt after Python source or dependency changes.

`docker compose stop` does not delete volumes. In contrast,
`docker compose down` removes containers and networks; **avoid
`docker compose down -v` unless you intentionally want to delete all persisted
PostgreSQL, Redis, and RabbitMQ data**.

### Full-stack startup

```bash
docker compose config -q
docker compose up -d --build
docker compose ps -a
```

Expected state:

- `postgres`, `redis`, `rabbitmq`: **Up (healthy)**.
- `api`: **Up (healthy)**.
- `dispatcher`, `worker`: **Up**, without restart loops.
- `migrate`: **Exited (0)** — a successful, one-time `alembic upgrade head`.

The API documentation is at <http://127.0.0.1:8000/docs>. The development
port is bound to `127.0.0.1`, not publicly exposed. If port `8000` is already
in use, set `API_HOST_PORT=8001` in `.env`, rerun Compose, and open
<http://127.0.0.1:8001/docs>.

Before switching to the full Compose stack, stop any manually started host
`uvicorn`, dispatcher, or worker processes. Compose cannot manage processes
launched outside of Docker; simultaneous runs may cause port collisions or
extra consumers.

### Host-mode development

To run Python locally while keeping only infrastructure containerized:

```bash
docker compose stop api dispatcher worker
docker compose up -d postgres redis rabbitmq
uv sync
uv run alembic upgrade head
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

If you need to debug the application processes individually, run these in
**separate terminals**, after stopping the corresponding Compose app services:

```bash
uv run uvicorn maintainer_lens.main:app --reload
uv run python -m maintainer_lens.outbox_dispatcher
uv run python -m maintainer_lens.worker
```

### Integration tests

Most database/RabbitMQ integration tests are **opt-in**, so a normal pytest
report with skipped tests does not mean the entire integration suite passed.
Start infrastructure and apply migrations before enabling them. Use disposable
development/test data, never a production database or broker.

```bash
# PostgreSQL integration tests
RUN_POSTGRES_INTEGRATION=1 uv run pytest tests/integration -v

# PostgreSQL + real RabbitMQ worker/outbox integration tests
RUN_POSTGRES_INTEGRATION=1 RUN_RABBITMQ_INTEGRATION=1 RUN_WORKER_E2E=1 uv run pytest tests/integration -v
```

Avoid running the ordinary Compose dispatcher concurrently against the same
test database during Outbox concurrency/delivery tests, because it can claim
the events under test. Keep the infrastructure containers running, but stop
the application containers when necessary.

## Continuous integration (GitHub Actions)

The workflow is defined in [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
It runs on `feat/**`, `fix/**`, and `docs/**` pushes, pull requests, updates to
`main`, and manual workflow dispatch.

There are **two independent CI jobs**:

1. **Lint, unit tests, and package:** Python 3.12, locked `uv` installation,
   Ruff lint/format, default pytest tests, and wheel/source-distribution builds.
   The default suite intentionally skips opt-in integration tests.
2. **Database, broker, and startup integration:** disposable PostgreSQL 17 with
   pgvector, Redis 7.4, and RabbitMQ 4.2 service containers; Alembic migrations;
   FastAPI lifespan/liveness startup validation; and pytest with all three
   opt-in integration flags (`RUN_POSTGRES_INTEGRATION`,
   `RUN_RABBITMQ_INTEGRATION`, `RUN_WORKER_E2E`) enabled.

The CI services use isolated, disposable credentials. They do not use developer
`.env` files, local persistent volumes, or production credentials.

**Before merging a PR, confirm both jobs are green under GitHub Actions/Checks.**
Creating a workflow file locally does not establish that CI is passing: it must
actually run on GitHub after pushing the feature branch. This CI does not
replace the local Compose/full GitHub API smoke test.

## Try a real GitHub issue

1. Start the complete stack and open `/docs`.
2. Call `POST /investigations` with:

   ```json
   {
     "repository": "encode/httpx",
     "issue_number": 3712
   }
   ```

3. Save the returned `task_id` (HTTP **202 Accepted**, initial status `pending`).
4. Call `GET /investigations/{task_id}` until `status` becomes `succeeded`.
5. Confirm `issue` is non-null and contains the GitHub title, body, state,
   source URL, and timestamps.

External GitHub availability and rate limits can affect completion time. If a
request fails, inspect `docker compose logs -f api dispatcher worker`.

## Operational notes and scope

- **Durability:** The investigation task and outbox event are committed in one
  PostgreSQL transaction. The dispatcher marks an event published only after
  broker confirmation.
- **Execution safety:** PostgreSQL claim tokens and expiring leases guard
  concurrent/late workers; delivery is at least once, **not exactly once**.
- **Local security:** Development service ports bind to localhost; this Compose
  stack is **not a hardened public-internet deployment**.
- **Data:** `postgres_data`, `redis_data`, and `rabbitmq_data` are persistent
  named volumes. Never use `down -v` as a routine shutdown command.
- **Current scope:** Public GitHub Issue retrieval and snapshot persistence.
  Authentication, repository source indexing, RAG, multi-agent reports, HITL,
  and shared cross-worker rate-limit coordination remain planned.

This README is the **current development and operations guide**, not final
release documentation. The project uses feature branches, pull-request review,
and squash merges; `main` is not a development branch.

## License

Apache-2.0. See [LICENSE](LICENSE).
