# MaintainerLens

**Evidence-grounded, asynchronous GitHub Issue investigation for open-source maintainers.**

MaintainerLens is an Apache-2.0 project under active development. V1 targets **public GitHub repositories** and a read-only, server-side investigation experience: reliable asynchronous Issue intake; source/documentation and historical Issue/PR retrieval; bounded LangGraph reasoning; evidence-linked conclusions; and, in later milestones, durable workflow recovery, memory, and human review. It is not an IDE code-editing assistant or an automatic source patcher.

## Current capabilities

- **Reliable backend:** HTTP submission, transactional PostgreSQL Outbox,
  confirmed RabbitMQ delivery, task claims/leases and fenced Issue snapshots.
- **Knowledge retrieval:** versioned source/document indexes, historical
  Issue/PR/comment preparation, semantic retrieval and optional source hybrid
  retrieval, with small live retrieval benchmarks.
- **Investigation application:** independently callable LangGraph investigation
  with structured cited findings, bounded follow-up, whole-Graph deadlines,
  cancellation handling and validated input/output.
- **Durable results:** an injected Snapshot → Graph → result processor,
  lease-fenced versioned JSONB storage and HTTP readback of completed results.

**Integration boundary:** the default Worker now invokes the Snapshot → Graph →
result processor using prepared History knowledge. Its backend
search profile has now expanded to at most three rounds, Chinese output and a
120-second Graph deadline. Optional explicit Source UUIDs and model-selected
follow-up tools are wired, and GET derives a Markdown report from saved analysis.
The earlier bounded History-only smoke passed per maintainer feedback; new
Source/full-chain regressions and paid smoke await execution. M4 is still in
progress; Checkpointer, HITL and cross-run memory are not implemented. Wiring tests with
fake I/O are not a live Worker-to-Graph investigation.

[Roadmap](docs/ROADMAP.md) · [Architecture](docs/ARCHITECTURE.md) ·
[Architecture decisions](docs/ADR/README.md)

## Architecture overview

### Existing production path

```text
Client → FastAPI POST /investigations
    → PostgreSQL (InvestigationTask + OutboxEvent in one transaction)
    → independent Outbox Dispatcher (publisher confirms)
    → RabbitMQ pending/retry/dead-letter queues
    → independent Worker (handler, task claim, claim token, lease renewal)
    → GitHub public Issue fetch + fenced IssueSnapshot persistence
    → History + optional explicit Source + bounded tool/analysis → fenced result commit
    → guarded task-success commit
    → FastAPI GET /investigations/{task_id}
```

The API, dispatcher and worker are independent long-running Python services. The database, Redis and RabbitMQ services run separately; Alembic migration is a one-shot job. Message processing has **at-least-once semantics**. Claim tokens, expiring leases, guarded writes and transactional Outbox handle duplicates and partial failures; these mechanisms do not promise exactly-once external LLM calls.

### Knowledge and investigation modules

```text
Pinned public repository → secure blob fetch → syntax-aware chunks
    → Embedding → PostgreSQL / pgvector versioned ready index
Question → source semantic/optional ILIKE keyword → RRF → SourceEvidence

GitHub Issues/PRs/comments → bounded ingestion + incremental sync
    → Embedding + PostgreSQL → semantic HistoryEvidence

Issue + evidence → LangGraph: initial History/Source RAG
    → cited structured LLM analysis
    → optional bounded query planning or whitelisted tool calling
    → deduplicated new evidence → reanalysis or early stop
```

Source retrieval uses **exact pgvector cosine search** (no HNSW in V1) with optional literal `ILIKE` keyword retrieval fused using RRF; History defaults to semantic search. HNSW/reranking/new search engines are deferred until evaluation demonstrates a need. Citations include stable source URLs/commit/line spans and history object identities. A model's free-text interpretation is not sufficient proof of source facts.

Initial source evaluation: eight bilingual queries (only two distinct relevant targets) achieved Recall@5 and MRR@5 of 1.000 for both source strategies. History evaluation: 40 candidate `encode/httpx` issues with 14 bilingual queries; semantic Top-1 92.9%, Recall@5 100%, MRR@5 0.964. **Small, incompletely representative tests are not population accuracy estimates.**

## Investigation results

`GET /investigations/{task_id}` returns task identity/status, an optional `issue`
snapshot, the selected `source_index_id`, an optional typed `result` and
`report_markdown` derived from that result. A completed Graph result includes the
structured preliminary analysis, complete returned History/Source evidence,
the actual latest analysis excerpts, terminal next step, search round and
routing reason.

Only `succeeded` tasks expose a stored result. Pending/running/failed tasks and
legacy snapshot-only successes return `result: null`. Unsupported/corrupt stored
results on a successful task produce HTTP 500 instead of silently hiding the
error. GET does not invoke providers or backfill old tasks.

Both `draft_preliminary_report` and `needs_more_evidence` are legitimate Graph
outcomes. Task success means execution and required writes completed, not that
the investigation established a root cause. The report deterministically formats
summary, cited findings, hypotheses, open questions and actual analysis excerpts.
It makes no extra model call and remains explicit about insufficient evidence. See the [architecture](docs/ARCHITECTURE.md) for execution,
validation and persistence boundaries.

The intended product flow is Issue URL → backend-managed knowledge preparation
and investigation → Markdown report, without asking users for index UUIDs.
Currently `/docs` is Swagger API documentation: POST accepts repository/Issue
number and an optional prepared Source UUID, then GET exposes asynchronous status
and output. URL-only intake and automatic bounded knowledge preparation are not
yet wired. A Source index ID identifies prepared knowledge; it is independent of
the investigation task ID and does not require a previous investigation task.

## Default Worker profile

Configure the existing `LLM_BASE_URL`, `LLM_MODEL_NAME`, `LLM_API_KEY`,
`EMBEDDING_BASE_URL`, `EMBEDDING_MODEL` and applicable embedding credentials in
your local environment. Dimensions and model must match prepared History
embeddings with provider identity `openai-compatible`. This identity is the
stored backend label, not a requirement to use a particular vendor.

The Worker validates required provider configuration before opening the broker
connection. Compose forwards these settings only to the Worker; API and
Dispatcher do not require provider credentials. An explicitly injected
investigator also bypasses default provider assembly.

The backend profile uses at most five hits per retriever, up to three search
rounds, Chinese analysis, a 120-second whole-Graph deadline and 60 seconds per
tool step. Tool Runner selects one allowed search per follow-up; Query Planner
remains an alternative application option. Pass optional `source_index_id` in
POST JSON to enable a prepared Source index. The Worker validates repository,
ready status and embedding identity before GitHub/retrieval; building indexes
use bounded retries and other invalid selections fail without fallback. It does not ingest history, refresh embeddings or build
source indexes automatically. Empty matching History produces a legitimate
`needs_more_evidence` result; it does not trigger automatic knowledge preparation.

Per attempt, initial retrieval makes one query embedding per enabled corpus;
at most two follow-up searches and three analyses can occur. Each executed tool
step additionally makes selection and feedback Chat requests: at most seven
Chat requests total. Duplicate queries or no new evidence stop early. Running the
default Worker can incur provider charges; existing bounded delivery retries
can repeat those requests. The
Graph deadline excludes GitHub snapshot acquisition and result commit.

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
connection URLs, plus the LLM/embedding settings needed by the default Worker.
**Do not commit `.env`, tokens, or credentials.** If existing
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

### GitHub API authentication (optional)

MaintainerLens supports an optional GitHub Personal Access Token (PAT) for accessing public repositories.

Unauthenticated GitHub REST API requests are typically limited to 60 requests per hour per IP address. Authenticated requests typically have a primary rate limit of 5,000 requests per hour per user.

To configure GitHub authentication:

1. Create a fine-grained PAT at [GitHub Personal Access Tokens](https://github.com/settings/personal-access-tokens).
2. For public repository access, no additional repository permissions are required.
3. Add the token to your local `.env` file:

   ```dotenv
   GITHUB_TOKEN=your_github_personal_access_token
   ```

4. If using Docker, apply the configuration:

   ```bash
   docker compose up -d --build
   ```

The token is optional. Without it, public GitHub API requests use the unauthenticated rate limit.

Never commit `.env` or expose tokens in source code, logs, or screenshots. The `.env.example` file documents the required configuration without containing real credentials.

Production deployments should use a dedicated secret-management mechanism.

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

### Graph execution and result regression tests

These tests use fake providers; they do not call paid LLM/embedding APIs.
Database tests require the existing migrated non-production PostgreSQL instance.

```bash
# Isolated application, shared contracts and HTTP behavior
RUN_POSTGRES_INTEGRATION=0 uv run pytest -q \
  tests/test_investigation_contracts.py \
  tests/agents/test_investigation_application_execution.py \
  tests/api/test_investigations.py \
  tests/test_worker_startup.py \
  tests/test_worker_production_wiring.py \
  tests/integration/test_investigation_processor_pipeline.py \
  tests/integration/test_investigation_result_readback.py

# Actual PostgreSQL persistence/processor/readback; external providers mocked
RUN_POSTGRES_INTEGRATION=1 uv run pytest -q \
  tests/integration/test_investigation_result_persistence.py \
  tests/integration/test_investigation_processor_pipeline.py \
  tests/integration/test_investigation_result_readback.py
```

### Explicit Source index validation

Source investigations select an existing ready index UUID and verify repository,
embedding provider/model/dimensions before query embeddings. The Source searcher
factory now requires `embedding_dimensions`. The read-only helper returns
immutable revision/chunker metadata; it does not build indexes or choose latest.
API submission, Task/Outbox, message publication and Worker now carry the selected
UUID. Older messages omitting it remain History-only and must match their task.

Task storage now includes optional `source_index_id` (native UUID). Apply the
additive migration manually before using the updated ORM with an existing
database; requests omitting the index continue to leave this field NULL. The UUID
has no foreign key, preserving the requested identity if an index is removed.
Execution must still validate index availability and ownership.

```bash
# Manual database upgrade; adds a column, not a new table
uv run alembic upgrade head

# Offline model/DDL checks, without loading Alembic env or connecting to services
RUN_POSTGRES_INTEGRATION=0 uv run pytest -q \
  tests/integration/test_investigation_task_source_index_schema.py

# After the upgrade: UUID persistence, lifecycle and retention checks
RUN_POSTGRES_INTEGRATION=1 uv run pytest -q \
  tests/integration/test_investigation_task_source_index_schema.py
```

```bash
# Isolated validation and Graph adapter behavior; no external services
RUN_POSTGRES_INTEGRATION=0 uv run pytest -q \
  tests/integration/test_investigation_source_index_selection.py \
  tests/agents/test_investigation_graph_source.py

# Migrated PostgreSQL/pgvector; embeddings are fake, no paid provider calls
RUN_POSTGRES_INTEGRATION=1 uv run pytest -q \
  tests/integration/test_investigation_source_index_selection.py \
  tests/integration/test_investigation_source_searcher_postgres.py
```

### Opt-in live History Worker smoke

The [live smoke test](tests/integration/test_worker_graph_live_smoke.py) uses
real GitHub, embedding/LLM endpoints, PostgreSQL/pgvector and RabbitMQ. It runs
the current default Worker without a Source selection on [sampleproject Issue #225](https://github.com/pypa/sampleproject/issues/225)
with one real historical fixture, [Issue #221](https://github.com/pypa/sampleproject/issues/221).
This is a plumbing smoke test, not a claim that the historical Issue explains
the investigated Issue. Either valid Graph terminal outcome is accepted.

Use an existing migrated non-production database and broker, with provider
settings already configured. The database must have no History records for
`pypa/sampleproject`; otherwise the test skips before provider calls. Do not run
History ingestion for that repository concurrently. The fixture inserts rather
than overwrites knowledge, then deletes only its task and unchanged inserted
History row; foreign keys clean up its snapshot/result/embedding records.

```bash
# Offline request-limit/diagnostic guards; the real smoke is skipped
RUN_LIVE_WORKER_GRAPH_SMOKE=0 uv run pytest -q \
  tests/integration/test_worker_graph_live_smoke.py

# Explicitly authorized paid smoke; all three flags are required
RUN_LIVE_WORKER_GRAPH_SMOKE=1 \
RUN_POSTGRES_INTEGRATION=1 \
RUN_RABBITMQ_INTEGRATION=1 \
uv run pytest -q -s tests/integration/test_worker_graph_live_smoke.py
```

Hard request hooks permit at most four GitHub requests, four embedding requests
(one document plus up to three searches) and seven Chat requests, each with
`max_tokens <= 1500`. The previously reported 4/2/1 success used the initial
single-round profile; that result does not validate the expanded profile.
The test has a 240-second operation deadline. Unique temporary broker queues
park retries without redelivery, and teardown cancels the Worker and removes
only test resources. Remote cancellation does not guarantee billing stops.
Diagnostics show counts/status/finish reason, not credentials or model text.

The test starts at task creation and real message publication; it does not
exercise POST/Outbox Dispatcher. GET uses the real route through an in-process
ASGI client, not a separately deployed API server. No migrations or GitHub
writes run. Normal tests/CI skip this paid case unless explicitly enabled.
The maintainer reported a successful bounded History-only run with normal model
completion; this does not establish Source integration or semantic accuracy.

### Knowledge-preparation budget profiles

`GET /investigations/budget-profiles` describes current choices without querying
task data or invoking providers. POST accepts `knowledge_budget_profile`, default
`standard`. The server resolves it once and stores a versioned `knowledge_budget`
JSONB snapshot with Task/Outbox submission; GET task details returns that stored
snapshot. Retries must consume stored numbers, never re-resolve a profile name.
Messages carry task identity rather than a second copy of this configuration.

| Profile | Source files | Source bytes | Source chunks | History parents | Preparation deadline |
| --- | ---: | ---: | ---: | ---: | ---: |
| `small` | 20 | 256 KiB | 100 | 1 | 300 seconds |
| `standard` (default) | 500 | 8 MiB | 2,000 | 3 | 900 seconds |
| `large` | 2,000 | 8 MiB | 4,000 | 5 | 1,800 seconds |
| `unlimited` | null | null | null | null | null |

For example, the current submission contract accepts:

```json
{
  "repository": "pypa/sampleproject",
  "issue_number": 225,
  "knowledge_budget_profile": "standard"
}
```

Null means no task-level preparation limit; it is not infinity/zero, and it does
not disable content validation, per-request transport controls or Graph's
three-round/120-second budget. Frontend code can discover choices through the
read-only endpoint; no frontend is implemented by this change.

Profile discovery, validation, submission snapshots and readback are implemented.
Automatic preparation execution and URL-only intake are still pending. In
particular, the existing 8 MiB in-memory downloader and bounded History helper
must be adapted before unlimited preparation is actually supported. Selecting a
profile currently records configuration; it does not enable the unwired path.
Paid acceptance continues to use a small repository rather than maximum budgets.

### Preparation mode and budget migration

The **not-yet-applied** `e8b3c7d9a204` revision now adds three task columns:
`auto_prepare_knowledge`, `prepared_source_index_id` and `knowledge_budget`,
plus mode/object checks. Apply this revised file manually before updated ORM
usage. Existing tasks/inserts receive false mode, no prepared index and the
original standard-v1 snapshot. No new table or subsequent revision is needed.
Do not toggle rows manually to enable an unwired automatic execution path.

```bash
uv run alembic upgrade head
uv run alembic current  # expected head: e8b3c7d9a204

# Offline contracts, DDL and API checks; no external services
RUN_POSTGRES_INTEGRATION=0 RUN_RABBITMQ_INTEGRATION=0 uv run pytest -q \
  tests/integration/test_investigation_knowledge_preparation_schema.py \
  tests/integration/test_investigation_budget_snapshot.py \
  tests/test_investigation_contracts.py \
  tests/api/test_investigations.py \
  tests/integration/test_investigation_result_readback.py \
  tests/test_investigation_source_contracts_and_reports.py

# After manual upgrade: defaults, checks, snapshots, retries and readback
RUN_POSTGRES_INTEGRATION=1 uv run pytest -q \
  tests/integration/test_investigation_knowledge_preparation_schema.py \
  tests/integration/test_investigation_budget_snapshot.py \
  tests/integration/test_investigation_task_source_index_schema.py \
  tests/integration/test_investigation_result_readback.py
```

### Complete Source/History investigation acceptance

These tests use a migrated disposable PostgreSQL/pgvector database and isolated
real RabbitMQ queues. The ordinary E2E cases fake only upstream providers and
cover POST, atomic Task/Outbox, real dispatch, default Worker, required writes,
pre-ACK database checks, duplicate delivery and GET/result/report readback.
Do not run a competing Dispatcher against the test database. A foreign Outbox
claim is rejected inside its transaction before publication.

```bash
# Offline UUID/report tests and real compiled Graph/provider protocol tests
RUN_POSTGRES_INTEGRATION=0 RUN_RABBITMQ_INTEGRATION=0 uv run pytest -q \
  tests/test_investigation_source_contracts_and_reports.py \
  tests/test_worker_production_wiring.py \
  tests/agents/test_investigation_execution.py \
  tests/integration/test_worker_graph_source_e2e.py

# Real PostgreSQL and RabbitMQ; providers are fake, paid case remains skipped
RUN_LIVE_SOURCE_WORKER_SMOKE=0 \
RUN_POSTGRES_INTEGRATION=1 RUN_RABBITMQ_INTEGRATION=1 uv run pytest -q \
  tests/integration/test_worker_graph_source_e2e.py

# Authorized paid acceptance: the fixture prepares its own Source/History data
RUN_LIVE_SOURCE_WORKER_SMOKE=1 \
RUN_POSTGRES_INTEGRATION=1 RUN_RABBITMQ_INTEGRATION=1 uv run pytest -q -s \
  tests/integration/test_worker_graph_source_e2e.py
```

The paid fixture needs no index UUID or manually submitted task. It resolves a
public sampleproject revision, fetches its manifest and verifies one small Python
blob, then chunks/embeds only one real Source excerpt using the existing writer.
The writer generates an index UUID; the test submits it internally through POST.
This is a deliberately partial smoke index, not a complete repository build.
A unique fixture version prevents reuse of existing indexes; teardown deletes
only its owned index, not any pre-existing knowledge. The test also requires an
empty sampleproject History corpus, inserts/embeds one real Issue (#221), and
investigates #225. Fixtures prepare knowledge; the production Worker still does
not automatically build it. Caps are GitHub 8, embeddings 6 and Chat 7,
with one input per embedding request,
1500 maximum output tokens per Chat request and a 300-second operation deadline.
Retries are parked; fixtures clean up only owned rows and queues. GET uses the
real route in-process. Valid `needs_more_evidence` is accepted. Tool selection
may be skipped after sufficient initial evidence, so a live pass alone does not
prove a multi-round path or semantic accuracy; fake-provider regressions force
those branches. Assistant-supplied tests have not been executed by the assistant.

The maintainer confirmed the bounded real Source+History smoke passed. Production
automatic knowledge preparation and URL-only intake remain separate pending work.

### Repository retrieval evaluation

The initial source evaluation dataset in `tests/fixtures/retrieval/sampleproject.json`
contains eight English/Chinese queries with manually labeled source evidence
at a fixed `pypa/sampleproject` commit. The evaluation package implements
Recall@K / MRR@K, evidence-anchor-to-chunk-key resolution, and a comparison
of semantic and automatic-hybrid retrieval.

```bash
# Pure Python evaluation tests (no external services)
uv run pytest tests/evaluation -q

# PostgreSQL integration: creates and cleans up its own test index/chunks
RUN_POSTGRES_INTEGRATION=1 uv run pytest \
  tests/integration/test_retrieval_anchor_resolution_postgres.py -q

# Opt-in real GitHub, PostgreSQL and embedding API evaluation (may incur costs)
RUN_POSTGRES_INTEGRATION=1 RUN_LIVE_RETRIEVAL_EVALUATION=1 uv run pytest \
  tests/integration/test_live_repository_retrieval_evaluation.py -q -s
```

The live smoke evaluation returned **Recall@5 = 1.000 and MRR@5 = 1.000**
for both retrieval strategies on all eight questions (two distinct source
targets, each ranked first). These results verify this small end-to-end
scenario; they do **not** establish performance on representative, complex
real-world Issues or show that hybrid search is generally better.
The live test uses the pinned historical commit, prepares an isolated index,
and cleans up its database records. It requires real network access, a
configured embedding provider, and a non-production PostgreSQL database.
It is disabled in ordinary pytest/CI runs unless explicitly enabled.

## Continuous integration (GitHub Actions)

The workflow is defined in [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
It runs on `feat/**`, `fix/**`, and `docs/**` pushes, pull requests, updates to
`main`, and manual workflow dispatch.

The workflow defines **two independent CI jobs**:

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
5. Confirm `issue` contains the GitHub snapshot and `result` contains the
   preliminary investigation. Without matching prepared History, a successful
   task can legitimately return `next_step: needs_more_evidence`.

This path uses real GitHub and configured embedding/LLM endpoints and may incur
charges. Prepare knowledge separately if you need evidence-backed findings.
Legacy snapshot-only successes still return a null result and are not backfilled.

External GitHub availability and rate limits can affect completion time. If a
request fails, inspect `docker compose logs -f api dispatcher worker`.

## Current milestone and contribution workflow

The M4 Worker binds prepared History, optional explicit Source and bounded tools to the existing Graph processor and root runner. Derived Markdown reports are implemented. New regression/live acceptance, semantic quality evaluation and CI/review remain. Before work, review `docs/ARCHITECTURE.md`, current source and tests; keep `agents/investigation_graph.py` strictly for node/edge composition. M5 will add persistent LangGraph checkpointing, human-in-the-loop decisions and trusted memory; M6 will focus on release-quality reliability, evaluation, UI and a Chinese README. V2 adds private repository authorization.

Use `feat/**` / `fix/**` / `docs/**` branches and reviewed squash-merge PRs; do not push to `main` directly. Before merging run Ruff, default pytest, appropriate opt-in integration tests and GitHub CI. Clearly label which integration levels actually ran. See [Roadmap](docs/ROADMAP.md) for milestones and [ADR](docs/ADR/) for established technical decisions.

## Security and operational scope

- Local Compose ports are intentionally bound to localhost; this stack is **not a hardened public deployment**.
- Keep `.env`, API keys, PATs, local `.local/` notes, and secrets out of Git, logs and reports.
- Development PostgreSQL, Redis and RabbitMQ use persistent named volumes; never run `docker compose down -v` as routine cleanup.
- A Docker `restart` does **not** rebuild application source; use `docker compose up -d --build` after code/dependency changes.
- Do not run host-based and container-based API/Worker/Dispatcher instances accidentally against the same test infrastructure.
- Live provider tests may incur API charges; keep them opt-in and clean up created test records.

## License

Apache-2.0. See [LICENSE](LICENSE).
