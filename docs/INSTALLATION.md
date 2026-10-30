# Installation and operations

[Documentation](README.md) · [Configuration](CONFIGURATION.md) · [Troubleshooting](TROUBLESHOOTING.md)

## Requirements

- Git and Docker Engine/Desktop with the Compose v2 plugin.
- Network access for container builds, public GitHub data and your configured providers.
- A compatible Chat model and Embedding service; see [protocol requirements](CONFIGURATION.md#model-protocol-requirements).

Docker builds the React frontend and Python application. You do not need host
Python, uv or Node.js for this deployment. The optional port-override examples
require Compose 2.24.4 or later.

## Fresh installation

```bash
git clone https://github.com/paulakkda-labs/maintainer-lens.git
cd maintainer-lens
cp .env.example .env
```

Edit `.env` with your database/broker credentials and both model configurations.
Do not leave the sample passwords in use. Existing data volumes require their
existing credentials; see [credential lifecycle](CONFIGURATION.md#database-and-broker-credentials).

```bash
docker compose config -q
docker compose up -d --build
docker compose ps -a
```

`config -q` checks configuration without printing its resolved values. The first
build needs to download dependencies. `up` starts PostgreSQL and RabbitMQ,
waits for their health checks, runs `alembic upgrade head` through the migration
job, then starts API, Dispatcher and Worker. It preserves existing data volumes.

| Service | Expected state | Purpose |
| --- | --- | --- |
| `postgres` | Running, healthy | Tasks, pgvector knowledge, checkpoints and results |
| `rabbitmq` | Running, healthy | Durable investigation delivery |
| `migrate` | Exited with code **0** | Successful one-shot schema migration |
| `api` | Running, healthy | Web pages and HTTP API |
| `dispatcher` | Running | Publishes committed Outbox work |
| `worker` | Running | Prepares knowledge and executes investigations |

A stopped `migrate` with exit code 0 is normal. A repeatedly restarting Worker
is not; inspect its configuration and logs rather than relying only on API health.

Open:

- [Investigation workspace](http://127.0.0.1:8000/)
- [Execution records](http://127.0.0.1:8000/logs)
- [Interactive API documentation](http://127.0.0.1:8000/docs)
- [RabbitMQ management](http://127.0.0.1:15672/), using `RABBITMQ_USER` / `RABBITMQ_PASSWORD`

```bash
curl --fail http://127.0.0.1:8000/health/live
```

The liveness response is `{"status":"ok"}`. It does not certify external model
availability. For a first investigation, use a small public repository and select
Small. The default UI language is English. [Usage guide](WEB_INTERFACE.md)

## Service commands

Run these from the repository root. If using a custom Compose override, apply
the same `-f` files and project name to every command.

| Task | Command |
| --- | --- |
| Build and start/update the stack | `docker compose up -d --build` |
| Start infrastructure only | `docker compose up -d postgres rabbitmq` |
| See all states, including migration | `docker compose ps -a` |
| Follow application logs | `docker compose logs -f --tail=100 api dispatcher worker` |
| Stop applications, retain infrastructure | `docker compose stop api dispatcher worker` |
| Stop all services, retain containers/data | `docker compose stop` |
| Start stopped services without rebuilding | `docker compose start` |
| Restart the same application containers | `docker compose restart api dispatcher worker` |
| Remove containers/network, retain named volumes | `docker compose down` |

`restart` does not install changed source, dependencies or environment settings.
Rebuild/recreate when applying those changes. **`docker compose down -v` deletes
the database and broker volumes**; it is not a routine upgrade or troubleshooting step.

Database and broker host ports default to 5432 and 5672; the API defaults to 8000.
For conflicts, custom bind addresses or another installation, follow
[IP/port configuration](CONFIGURATION.md#custom-ip-addresses-and-ports). Never
share an ordinary deployment's volumes with a disposable acceptance environment.

## Upgrading an existing installation

Back up persistent data and export any application logs you need before replacing
containers. Avoid interrupting active investigations or pending human decisions.
Code/runtime changes can make older frozen Runs incompatible; their records
remain available, and URL resolution can create a new investigation when eligible.

After updating your checkout to the intended revision:

```bash
docker compose up -d postgres rabbitmq
docker compose stop api dispatcher worker
docker compose build api
docker compose run --rm --no-deps migrate
docker compose up -d --no-deps --force-recreate api dispatcher worker
docker compose ps -a
```

The applications share one build definition and image. The explicit migration
step uses the newly built image and the Compose database credentials. If it fails,
resolve the failure before restarting the applications. No data reset is needed.
Do not run incompatible API and Worker versions together.

## Optional: Python services on the host

This mode needs Python 3.12+, uv and, for the Web UI, Node.js 22.12+.
Use the host-mode `DATABASE_URL` and `RABBITMQ_URL` from `.env`, pointing to the
published localhost ports. Keep the Docker application processes stopped:

```bash
docker compose stop api dispatcher worker
docker compose up -d postgres rabbitmq
uv sync --locked --dev
uv run --locked alembic upgrade head
```

Build Web assets once, from a separate terminal:

```bash
cd web
npm ci --ignore-scripts --no-audit --no-fund
npm run build
```

Then run each Python process in its own terminal **from the repository root**:

```bash
uv run --locked uvicorn maintainer_lens.main:app --host 127.0.0.1 --port 8000
```

```bash
uv run --locked python -m maintainer_lens.outbox_dispatcher
```

```bash
uv run --locked python -m maintainer_lens.worker
```

FastAPI serves `web/dist` relative to the working directory. Without built assets,
the Web entries return 503 while the API/Swagger can remain available. Python
wheels alone do not contain the separate frontend build. See
[development](DEVELOPMENT.md) for Vite and test workflows.
