# tracing-tax

Research stand for the coursework *"Optimizing observability systems: the impact
of trace sampling strategies on microservice architecture load"*.

The stand is a small chain of FastAPI services, traced with OpenTelemetry
zero-code instrumentation:

```
                                       ┌──► inventory ──► Redis
curl ──► gateway ──► orders ───────────┤
                                       └──► payments

all services ──OTLP/gRPC──► OTel Collector ──OTLP/gRPC──► Jaeger v2 (UI :16686)
```

### What the services mimic

A checkout in an online shop: one request places an order for 1 book and 2 pens.
The logic is deliberately minimal. The services exist to produce a realistic
chain of calls and spans, not real business behaviour.

| Service | In a real shop | Here |
|---|---|---|
| `gateway` | Public entry point (auth, routing) | Forwards the request to `orders` |
| `orders` | Creates the order and coordinates the checkout | 20 ms of "work", then calls `inventory` and `payments`, in that order |
| `inventory` | Reserves stock so two customers can't buy the last item | Adds the quantities to running counters in Redis (`reserved:book`, `reserved:pen`); no stock limits |
| `payments` | Charges the card through a payment provider | Waits 30 ms (the "bank call") and returns a random payment ID |
| `redis` | Inventory database | In-memory counters, no persistence |

### Pinned versions

| Component | Image / package | Version |
|---|---|---|
| Services runtime | `python` | `3.14.10-slim-trixie` |
| Web framework | `fastapi` / `uvicorn` / `httpx` | `0.163.1` / `0.54.0` / `0.28.1` |
| Redis / client | `redis` image / `redis` (redis-py) | `8.10.3-alpine3.23` / `8.1.0` |
| OTel Python SDK / instrumentation | `opentelemetry-sdk` / `opentelemetry-distro` | `1.45.2` / `0.66b2` |
| Collector | `otel/opentelemetry-collector-contrib` | `0.165.0` |
| Tracing backend | `cr.jaegertracing.io/jaegertracing/jaeger` | `2.23.0` |

Services are a separate uv project (`services/pyproject.toml`). Their full
dependency tree, including transitive packages and hashes, is locked in
`services/uv.lock`, and the image is built with `uv sync --locked`
(uv `0.12.23`, used only at build time).

## Requirements

- Docker with Compose v2
- Python 3.10+ on the host (only for the stdlib verification script)
- [uv](https://docs.astral.sh/uv/) `0.12.x`, only for local development and IDE support
- Free host ports `8080` (gateway) and `16686` (Jaeger UI)

## Run

```bash
docker compose up -d --build
```

The first build takes a minute or two. Wait until `docker compose ps` shows all
services as `healthy`, or use `docker compose up -d --build --wait`.

Stop and remove everything with `docker compose down`. Jaeger uses in-memory
storage, so this also deletes all traces.

## Verify

### Automated

```bash
python3 scripts/verify_trace.py
```

The script sends one request to the gateway and finds its trace through the
Jaeger query API (`/api/v3`). It prints the span tree, checks that every
service-to-service call and every Redis command is linked correctly, and prints a
direct link to the trace in the UI. Expected output (`http send/receive`
INTERNAL spans omitted here):

```
trace fb7324c4...: 19 spans, 4 services

  gateway: GET /order/{order_id}                       SERVER      65.6 ms
    gateway: GET                                       CLIENT      63.8 ms
      orders: GET /orders/{order_id}                   SERVER      61.0 ms
        orders: POST                                   CLIENT       4.1 ms
          inventory: POST /reservations                SERVER       1.6 ms
            inventory: INCRBY                          CLIENT       0.3 ms
            inventory: INCRBY                          CLIENT       0.2 ms
        orders: POST                                   CLIENT      33.7 ms
          payments: POST /payments                     SERVER      31.1 ms

  [PASS] gateway: exactly one SERVER span
  ...
  [PASS] single root span: gateway SERVER
  [PASS] orders SERVER <- gateway CLIENT <- gateway SERVER
  [PASS] inventory SERVER <- orders CLIENT <- orders SERVER
  [PASS] payments SERVER <- orders CLIENT <- orders SERVER
  [PASS] 2 redis CLIENT spans, children of inventory SERVER
  [PASS] every non-root span has its parent inside the trace
```

### Manual

```bash
curl -i http://localhost:8080/order/42
```

Then open the Jaeger UI at <http://localhost:16686>:

1. **Search** tab → *Service*: `gateway` → *Find Traces*.
2. Open the newest trace `gateway: GET /order/{order_id}`. It shows 4 services and
   19 spans:
   - `gateway  GET /order/{order_id}`: root SERVER span (FastAPI)
     - `gateway  GET`: CLIENT span of the outgoing httpx call
       - `orders  GET /orders/{order_id}`: SERVER span
         - `orders  POST` → `inventory  POST /reservations` → 2 × `INCRBY` (Redis)
         - `orders  POST` → `payments  POST /payments`
   - Each service also adds 2–3 ASGI INTERNAL spans (`http send` / `http receive`).
3. The **Trace Graph** view (top-right dropdown) shows the whole call graph.

Context propagates between services through the W3C `traceparent` header, which
the httpx and FastAPI instrumentations inject and extract automatically.

## Configuration

All knobs are environment variables read by `docker-compose.yml`. Pass them on the
command line, e.g. `ORDERS_ERROR_RATE=0.1 docker compose up -d`, or put them in a
`.env` file (which git ignores).

### Sampling (standard OTel SDK env vars only; there is no sampling code)

| Variable | Default | Examples |
|---|---|---|
| `OTEL_TRACES_SAMPLER` | `parentbased_always_on` | `always_on`, `always_off`, `parentbased_traceidratio` |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | `0.1`, `0.01` (ratio for `*traceidratio`) |

```bash
# head-based 10%
OTEL_TRACES_SAMPLER=parentbased_traceidratio OTEL_TRACES_SAMPLER_ARG=0.1 docker compose up -d
```

`scripts/verify_trace.py` assumes 100% sampling.

### Fault injection

| Variable | Default | Meaning |
|---|---|---|
Each service has its own variables, prefixed with its name (`GATEWAY_`, `ORDERS_`,
`INVENTORY_`, `PAYMENTS_`):

| Variable | Default | Meaning |
|---|---|---|
| `<SERVICE>_ERROR_RATE` | `0` | Probability (0..1) of answering HTTP 500 |
| `<SERVICE>_SLOW_RATE` | `0` | Probability (0..1) of adding extra delay |
| `ORDERS_WORK_MS` | `20` | Simulated work in `orders` |
| `PAYMENTS_WORK_MS` | `30` | Simulated payment provider call in `payments` |

Inside the containers these map to `ERROR_RATE`, `SLOW_RATE`, `SLOW_DELAY_MS`
(default 500) and `WORK_MS` (see `services/common/faults.py`).

A failure anywhere downstream propagates up: the failing service answers `500`,
and every caller above it answers `502` (`services/common/downstream.py`). All
server spans on that path get status `ERROR`. A slow request only adds latency
and does not change the span status.

### Collector config

`COLLECTOR_CONFIG` selects `collector/configs/<name>.yaml` (default: `passthrough`).
Per-scenario configs (e.g. tail sampling) go in that folder.

## Repository layout

```
services/
  Dockerfile           # one image for all services, selected via SERVICE build arg
  pyproject.toml       # uv project for the services: direct deps, pinned
  uv.lock              # full locked dependency tree (used by the image build)
  common/faults.py     # ERROR_RATE / SLOW_RATE fault injection
  common/downstream.py # HTTP calls to other services, failures -> 502
  gateway/app.py       # GET /order/{id} -> orders
  orders/app.py        # GET /orders/{id} -> inventory, payments
  inventory/app.py     # POST /reservations, INCRBY per item in Redis
  payments/app.py      # POST /payments, simulated work
collector/configs/     # one OTel Collector config per scenario
jaeger/config.yaml     # Jaeger v2 config (OTLP in, in-memory storage, UI)
scripts/verify_trace.py
docker-compose.yml
pyproject.toml         # uv project (non-package) for host-side tooling
```

## Notes

- Services run under `opentelemetry-instrument uvicorn app:app` with one worker
  and no `--reload`. Both of those options break zero-code instrumentation.
- Instrumentation packages are pinned explicitly in `services/pyproject.toml` rather than
  installed with `opentelemetry-bootstrap -a install`, which resolves versions at
  build time.
- Only traces are exported (`OTEL_METRICS_EXPORTER=none`, `OTEL_LOGS_EXPORTER=none`),
  so measured overhead comes from tracing alone. `/health` is excluded from tracing.
- All services, Redis and the collector share the same limits (`cpus: 1.0`,
  `mem_limit: 512m`), defined once in `docker-compose.yml`.
- Redis runs purely in memory (no RDB/AOF persistence), so disk I/O doesn't add
  noise to the measurements.
- The `inventory` health check does not ping Redis. Excluding `/health` from
  tracing only drops the HTTP span, so a Redis `PING` would still create a
  standalone trace on every probe.
- Service dependencies and host tooling are separate uv projects with separate
  locks, so upgrading analysis or load tools can never change what runs inside
  the measured services.

## Development (IDE setup)

There are two independent Python environments:

| Environment | Created by | Used for |
|---|---|---|
| `services/.venv` | `cd services && uv sync` | service code, same versions as in the containers |
| `.venv` (root) | `uv sync` | host-side tooling (load, experiments, analysis) |

**PyCharm**: open the repo root as the project, then:

1. *Settings → Project → Python Interpreter*: set the root `.venv`.
2. *File → Open…* `services/` and choose **Attach** to add it to the current
   window as a second module. Set its interpreter to `services/.venv`
   (*Add Interpreter → Existing → services/.venv/bin/python*).
3. Right-click `services/common` and choose *Mark Directory as → Sources Root*,
   so that `from faults import ...` and `import downstream` resolve. In the image, `common/` and the
   service folder are copied into the same directory.

**VS Code**: select `services/.venv/bin/python` as the interpreter when you
work on the services, and add `"python.analysis.extraPaths": ["services/common"]`
to `.vscode/settings.json`.

**Updating dependencies** (then rebuild with `docker compose build`):

```bash
cd services
uv add 'fastapi==<new version>'             # change a direct dependency
uv lock --upgrade-package grpcio            # bump one transitive dependency
uv sync                                     # refresh the local services/.venv
```

The image build fails if `uv.lock` is out of date with `pyproject.toml`.
