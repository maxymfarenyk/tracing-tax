# tracing-tax

Research stand for the coursework *"Optimizing observability systems: the impact
of trace sampling strategies on microservice architecture load"*.

**Iteration 1** sets up a minimal end-to-end tracing pipeline:

```
curl ──► gateway ──HTTP──► orders
           │                 │
           └──── OTLP/gRPC ──┴──► OTel Collector ──OTLP/gRPC──► Jaeger v2 (UI :16686)
```

| Component | Image / package | Version |
|---|---|---|
| Services runtime | `python` | `3.14.10-slim-trixie` |
| Web framework | `fastapi` / `uvicorn` / `httpx` | `0.163.1` / `0.54.0` / `0.28.1` |
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

The first build takes a minute or two. Wait until `docker compose ps` shows the
`gateway` and `orders` services as `healthy`, or use `docker compose up -d --build --wait`.

Stop and remove everything with `docker compose down`. Jaeger uses in-memory
storage, so this also deletes all traces.

## Verify

### Automated

```bash
python3 scripts/verify_trace.py
```

The script sends one request to the gateway and finds its trace through the
Jaeger query API (`/api/v3`). It then checks that the trace contains spans from
both services with correct parent-child links, and prints a direct link to the
trace in the UI. Expected output:

```
trace 77ba757c...: 7 spans
  gateway  SERVER   GET /order/{order_id}                 26.5 ms  parent=<root>
  gateway  CLIENT   GET                                   24.1 ms  parent=gateway:GET /order/{order_id}
  orders   SERVER   GET /orders/{order_id}                21.3 ms  parent=gateway:GET
  orders   INTERNAL GET /orders/{order_id} http send       0.0 ms  parent=orders:GET /orders/{order_id}
  ...
  [PASS] gateway SERVER span is the trace root
  [PASS] gateway CLIENT span is a child of gateway SERVER span
  [PASS] orders SERVER span is a child of gateway CLIENT span
  [PASS] every non-root span has its parent inside the trace
```

### Manual

```bash
curl -i http://localhost:8080/order/42
```

Then open the Jaeger UI at <http://localhost:16686>:

1. **Search** tab → *Service*: `gateway` → *Find Traces*.
2. Open the newest trace `gateway: GET /order/{order_id}`. It shows 2 services and
   7 spans:
   - `gateway  GET /order/{order_id}`: root SERVER span (FastAPI)
     - `gateway  GET`: CLIENT span of the outgoing httpx call
       - `orders  GET /orders/{order_id}`: SERVER span in `orders` (≈ `WORK_MS`)
         - 2 × `http send`: ASGI internal spans
     - 2 × `http send`: ASGI internal spans
3. The **Trace Graph** view (top-right dropdown) shows the `gateway → orders` call.

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
| `GATEWAY_ERROR_RATE`, `ORDERS_ERROR_RATE` | `0` | Probability (0..1) of answering HTTP 500 |
| `GATEWAY_SLOW_RATE`, `ORDERS_SLOW_RATE` | `0` | Probability (0..1) of adding extra delay |
| `ORDERS_WORK_MS` | `20` | Baseline simulated work in `orders` |

Inside the containers these map to `ERROR_RATE`, `SLOW_RATE`, `SLOW_DELAY_MS`
(default 500) and `WORK_MS` (see `services/common/faults.py`). When `orders`
fails, `gateway` returns `502`. Both server spans then get status `ERROR`.

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
  gateway/app.py       # GET /order/{id} -> orders
  orders/app.py        # GET /orders/{id}, simulated work
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
- `gateway`, `orders` and the collector share the same limits (`cpus: 1.0`,
  `mem_limit: 512m`), defined once in `docker-compose.yml`.
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
   so that `from faults import ...` resolves. In the image, `common/` and the
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
