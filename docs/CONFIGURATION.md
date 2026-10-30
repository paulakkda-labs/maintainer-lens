# Configuration

[Documentation](README.md) · [Installation](INSTALLATION.md) · [Usage](WEB_INTERFACE.md)

Copy [`.env.example`](../.env.example) to `.env` at the repository root. The
example is the configuration template; real credentials stay in the ignored
`.env`, never in browser code, screenshots or version control. Restarting a
container does not reload its environment: recreate affected applications after changes.

## Model protocol requirements

The server uses OpenAI-compatible HTTP endpoints; a provider or model name alone
does not establish compatibility. The default Worker needs:

| Operation | Required behavior |
| --- | --- |
| Analysis | `/chat/completions`, non-streaming `json_object` output, `max_tokens` and a normal `stop` completion |
| Follow-up tool selection | Function calling with exactly one allowed tool and `tool_choice: required` |
| Tool-result acknowledgment | Assistant/tool correlation IDs and `tool_choice: none` |
| Thinking control | The current adapters send `thinking: {"type":"disabled"}`; the endpoint must accept this field |
| Embedding | `/embeddings`, ordered vector responses with the configured dimensions |

Choose endpoints/models implementing these behaviors, or adapt the provider
boundary before using an incompatible service. The UI does not select models or
collect provider keys; deployment configuration controls them. No specific vendor
is required by the settings contract, and compatibility is not guaranteed for
every service advertising an OpenAI-compatible API.

Use a base URL such as `https://your-provider.example/v1`, **without** appending
`/chat/completions` or `/embeddings`; the clients append their operation paths.
LLM and Embedding may use different endpoints, keys and providers.

## LLM settings

| Variable | Meaning | Default |
| --- | --- | --- |
| `LLM_BASE_URL` | Compatible Chat API base URL | Required for the Worker |
| `LLM_MODEL_NAME` | Provider's model identifier | Required |
| `LLM_API_KEY` | Server-side Chat API key | Required |
| `LLM_MAX_OUTPUT_TOKENS` | Initial output allowance per analysis request | `4096` |
| `LLM_REVIEW_MAX_OUTPUT_TOKENS` | Optional maximum that an operator may approve after truncation | Empty: no additional application ceiling |

The review ceiling must be at least the initial allowance when set. It does not
discover or override provider capabilities. Approvals always specify a finite
positive integer; the actual model's output/context limits still apply.

A normally finished, nonempty response that fails JSON/schema validation can
receive **one** corrective completion using the same evidence, language and
per-call allowance. A second invalid candidate fails. Unknown citations, provider
errors and cancellation do not trigger this correction. `length` still uses the
existing output-budget review path; partial JSON is never accepted as a report.

## Embedding settings

| Variable | Meaning | Default/example |
| --- | --- | --- |
| `EMBEDDING_BASE_URL` | Embedding API base URL | Template contains a provider example |
| `EMBEDDING_MODEL` | Embedding model identifier | Template contains a model example |
| `EMBEDDING_API_KEY` | Server-side Embedding API key | Configure for your endpoint |
| `EMBEDDING_DIMENSIONS` | Expected vector length, checked on responses and index reuse | `1024` |
| `EMBEDDING_MAX_BATCH_SIZE` | Maximum number of texts per client batch | `20` |
| `EMBEDDING_REQUEST_DIMENSIONS` | Optional explicit `dimensions` request parameter | Empty: omit it |

Leave request dimensions empty unless your provider supports that parameter.
When supplied, it must match expected dimensions. The template's model/provider
values are examples, not a requirement to use that service.

Source indexes and History embeddings are isolated by repository and embedding
identity. Changing model, dimensions or endpoint may require new compatible
knowledge; vectors from another configuration are not silently reused. Model
and execution changes can also create new investigations instead of resuming old
checkpoints. API and Worker must use matching execution settings.

## GitHub access

`GITHUB_TOKEN` is optional for public repositories and recommended for ingestion.
An unauthenticated request uses GitHub's lower API rate limit. Use a token with
the public read access needed by your account/organization policy; V1 does not
use it to write repository code or open PRs. A token does not enable private
repository support in V1.

## Database and broker credentials

Set these in `.env` **before the first start**:

| Service | Variables |
| --- | --- |
| PostgreSQL | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` |
| RabbitMQ | `RABBITMQ_USER`, `RABBITMQ_PASSWORD`, `RABBITMQ_VHOST` |
| Host-mode Python | `DATABASE_URL`, `RABBITMQ_URL` |

The bundled Compose file generates container URLs using service names `postgres`
and `rabbitmq`. Host-mode Python reads the two URL variables directly, so keep
their usernames/passwords/database/vhost synchronized with the infrastructure.

The Compose URL templates do not percent-encode credentials. Use strong,
URL-safe credentials with the bundled templates. Reserved URL characters require
correctly encoded connection URLs in an adjusted deployment configuration.
The bundled RabbitMQ vhost convention uses a URL-safe name such as `maintainer_lens`.

**Changing `.env` does not rotate credentials in existing persistent volumes.**
Keep the existing credentials for an existing installation, or rotate them
through the database/broker and update connection settings together. Do not
delete volumes to apply a routine password or code change.

## Custom IP addresses and ports

### Default mappings

| Service | Host binding | Container destination |
| --- | --- | --- |
| API / Web | `127.0.0.1:8000` | `8000` |
| PostgreSQL | `127.0.0.1:5432` | `5432` |
| RabbitMQ AMQP | `127.0.0.1:5672` | `5672` |
| RabbitMQ management | `127.0.0.1:15672` | `15672` |

Dispatcher, Worker and migration job do not publish ports. Container-to-container
connections continue to use service DNS names and internal ports, regardless of
the host mappings.

### Change only the API port

Add this supported Compose variable to `.env` and recreate the API:

```dotenv
API_HOST_PORT=18000
```

```bash
docker compose up -d --no-deps --force-recreate api
```

Then open `http://127.0.0.1:18000`. `API_HOST_PORT` changes only the published API
port; it is not a Python setting and does not change the container's port 8000.

### Change any service's published address or port

Keep local overrides outside version control. From the repository root:

```bash
mkdir -p .local
```

Create `.local/compose.ports.yaml` with:

```yaml
services:
  api:
    ports: !override
      - "127.0.0.1:18000:8000"
  postgres:
    ports: !override
      - "127.0.0.1:15432:5432"
  rabbitmq:
    ports: !override
      - "127.0.0.1:5673:5672"
      - "127.0.0.1:15673:15672"
```

Use **both files for every command**:

```bash
docker compose --env-file .env -f compose.yaml -f .local/compose.ports.yaml config -q
docker compose --env-file .env -f compose.yaml -f .local/compose.ports.yaml up -d --build
```

`!override` replaces the full port list instead of keeping the original bindings.
It requires Compose 2.24.4+. See [Docker's merge rules](https://docs.docker.com/reference/compose-file/merge/#replace-value).
Use the same flags for `ps`, `logs`, `stop` and upgrades; omitting the override
can reapply the default mappings. Avoid printing a full resolved Compose config,
which includes credentials.

For host-mode Python with these mappings, update `DATABASE_URL` to port 15432
and `RABBITMQ_URL` to port 5673. Docker-mode URLs remain unchanged. To bind a
particular host interface, replace the leftmost `127.0.0.1` with an address
assigned to that host. Keep the rightmost container ports unchanged and keep
Uvicorn bound to `0.0.0.0` **inside** its container.

V1 has no authentication/user isolation. Broader network exposure needs access
controls and TLS appropriate to your deployment; keep database/broker bindings
private. Browser/API traffic is same-origin, so an ordinary published API port
change does not require a separate frontend build or CORS setting.

For an isolated second installation, also use a distinct `-p` project name and
nonconflicting bindings on **all** commands. This isolates Compose-managed
networks/volumes; a different API port alone does not isolate data.

## Knowledge-preparation budgets

The UI/API select a profile; the resolved limits are persisted on the Task.
Standard is the default. Start with Small for a small-repository trial.

| Profile | Source files | Total source bytes | Chunks | History items | Preparation deadline |
| --- | ---: | ---: | ---: | ---: | ---: |
| `small` | 20 | 256 KiB | 100 | 1 | 300 s |
| `standard` | 500 | 8 MiB | 2,000 | 3 | 900 s |
| `large` | 2,000 | 8 MiB | 4,000 | 5 | 1,800 s |
| `unlimited` | No task limit | No task limit | No task limit | No task limit | No task limit |

The Chinese UI labels this selector “Token 消耗预算”; technically these are
**knowledge-preparation limits**, not a token balance or a monetary spending cap.
Large does not increase the byte limit over Standard. Unlimited removes only
task-level preparation limits; content checks, network deadlines and Graph
controls remain. Larger profiles can increase downloads, latency and model cost.

The default Worker uses three search rounds, a 120-second active Graph-segment
deadline, a 60-second tool-step deadline, and up to five excerpts per corpus for
analysis. A full three-round tool path can make ten Chat calls when every analysis
needs its one correction; without corrections the maximum is seven. Recovery can
repeat uncommitted external work. None of these limits guarantees a currency cap.

These Graph controls are backend execution defaults, not additional `.env`
options. [Human decisions and waiting states](WEB_INTERFACE.md#human-review)
describe explicit budget increases and the separate recovery limits.
