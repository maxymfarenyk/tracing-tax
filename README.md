# tracing-tax

Research stand for the coursework *"Optimizing observability systems: the impact
of trace sampling strategies on microservice architecture load"*.

It compares trace sampling strategies (head-based, tail-based, adaptive) under
load and measures their cost for the services (CPU, RAM, latency), the network
and storage, and how many "interesting" traces (errors, slow requests) they
keep.

```
                                       ┌──► inventory ──► Redis
Locust ──► gateway ──► orders ─────────┤
                                       └──► payments

all services ──OTLP/gRPC──► OTel Collector ──OTLP/gRPC──► Jaeger v2 (UI :16686)

cAdvisor (per-container CPU/RAM/network) ─┐
OTel Collector self-metrics ──────────────┴──► Prometheus (:9090) ──► Grafana (:3000)
```

The services are small FastAPI apps traced with OpenTelemetry zero-code
instrumentation. One request is a shop checkout (19 spans across 4
services): `orders` does 20 ms of work, reserves stock in `inventory`
(Redis) and charges the card in `payments` (30 ms).

| Document | Content |
|---|---|
| [docs/results.md](docs/results.md) | full results, findings, limitations |
| [docs/experiment.md](docs/experiment.md) | how the runner and the analysis work, CSV columns |
| [docs/stand.md](docs/stand.md) | pinned versions, verification, monitoring, load, configuration, design notes, IDE setup |

## Requirements

- Docker with Compose v2
- Python 3.10+ on the host (stdlib only, for the scripts)
- [uv](https://docs.astral.sh/uv/) `0.12.x`, for the analysis (pandas, matplotlib)
- Free ports `8080`, `16686`, `9090`, `3000`, `8089`
- If your Docker daemon uses a non-default `data-root`
  (`docker info --format '{{.DockerRootDir}}'`), put it in a `.env` file so
  cAdvisor can read it, e.g. `DOCKER_DATA_ROOT=/mnt/hdd/docker-data`.

## Run

```bash
docker compose up -d --build --wait
python3 scripts/verify_trace.py        # one request, checks the whole trace
```

| UI | URL |
|---|---|
| Jaeger (traces) | <http://localhost:16686> |
| Grafana (resource dashboard) | <http://localhost:3000> |
| Prometheus | <http://localhost:9090> |
| Locust (with `--profile load`) | <http://localhost:8089> |

Stop with `docker compose down` (Jaeger is in-memory, so traces are lost).

## Scenarios

Each scenario is an env file in `scenarios/`; switching needs no rebuild.

| Scenario | Sampling |
|---|---|
| A | no tracing: SDK and instrumentation disabled (baseline) |
| B | head-based 100% (`always_on`) |
| C | head-based 10% (`parentbased_traceidratio`, `0.1`) |
| D | tail-based in the collector: services export 100%, the collector keeps errors, traces > 500 ms and 10% of the rest |
| E | adaptive head-based: keeps every trace up to 15 traces/s, ~15 traces/s above that ([details](docs/stand.md#adaptive-sampler-scenario-e)) |

```bash
docker compose --env-file .env --env-file scenarios/C.env up -d --wait
```

`--env-file` replaces the default `.env`, so pass `.env` first if you have
one, or leave it out. `scenarios/faults.env` adds the fault injection used in
the experiment: `payments` fails 5% of requests and delays another 5% by
500 ms.

## Experiment

```bash
python3 scripts/run_experiment.py      # A-E x low/high load x 3 reps -> results/raw/runs.csv
uv run analysis/analyze.py             # -> results/analysis/ (table + charts)
```

30 runs of 30 s warm-up + 2 min measured, at 10 and 30 RPS; about 1 h 45 min.
Every run restarts the measured containers. If interrupted, run the same
command again and it continues. Keep the host otherwise idle, and a laptop
on AC power: on battery the CPU is slower and the numbers are not comparable.
Details: [docs/experiment.md](docs/experiment.md).

## Results

At 30 RPS, mean ± SD over 3 runs (full table:
[summary.md](results/analysis/summary.md), discussion:
[docs/results.md](docs/results.md)):

| Scenario | CPU services, cores | p50, ms | Net to collector, KiB/s | Spans stored/s | RAM collector, MiB | Errors kept, % |
|---|---:|---:|---:|---:|---:|---:|
| A: no tracing | 0.394 ± 0.009 | 174 ± 2 | 0.1 | 0 | 40.2 ± 0.3 | – |
| B: head 100% | 0.575 ± 0.010 | 232 ± 10 | 133.2 ± 0.8 | 545 ± 10 | 61.4 ± 1.1 | 100 |
| C: head 10% | 0.506 ± 0.008 | 209 ± 9 | 14.3 ± 0.7 | 56 ± 4 | 46.7 ± 0.2 | 9.5 ± 2.0 |
| D: tail | 0.587 ± 0.011 | 236 ± 17 | 130.8 ± 3.8 | 100 ± 4 | 69.2 ± 0.7 | 100 |
| E: adaptive | 0.528 ± 0.015 | 211 ± 9 | 65.8 ± 1.4 | 269 ± 9 | 56.0 ± 0.7 | 48.9 ± 1.1 |

![Service CPU](results/analysis/cpu_services.png)
![Share of interesting traces kept](results/analysis/traces_kept.png)

- **Full tracing costs** +46% service CPU, +33% median latency and +25%
  service RAM compared with no tracing.
- **Head sampling at 10%** cuts network and storage by ~90%, but only 38% of
  the CPU overhead: the instrumentation runs on every request, sampled or
  not. It keeps only ~10% of error and slow traces.
- **Tail sampling** keeps 100% of error and slow traces and stores 82% fewer
  spans, but service CPU and network stay at the 100% level, and the
  collector needs 13% more RAM.
- **Adaptive sampling** keeps everything at low load and caps itself at
  ~14 traces/s at high load (half the traffic and storage), but drops errors
  as blindly as head sampling.

**Conclusion.** The hypothesis is confirmed. Head-based sampling reduces
overhead but blindly drops error traces; the overhead it removes is mainly
network and storage, while service CPU drops only partly. Tail-based sampling
keeps the interesting traces but saves nothing on the services or the network
and costs collector memory.

## Demo

Run load, switch C → D → E without rebuilding, and watch Grafana and Jaeger.
Leave out `--env-file .env` if you have no `.env`.

```bash
ENV="--env-file .env --env-file scenarios/faults.env"
docker compose $ENV --env-file scenarios/C.env --profile load up -d --build --wait
```

1. Locust <http://localhost:8089>: start **30 users, spawn rate 30**.
2. Grafana <http://localhost:3000>: *Collector spans* (~55 spans/s sent) and
   *Network into collector / s* (~14 KiB/s).
3. Jaeger <http://localhost:16686>: service `gateway`, *Tags* `error=true`,
   last 5 minutes. Few error traces: ~90% were dropped at the source.
4. Tail sampling (D); Locust keeps running, a few requests fail during the
   restart:

   ```bash
   docker compose $ENV --env-file scenarios/D.env --profile load up -d --wait
   ```

   Network into the collector and *Spans accepted / s* go up to the 100%
   level (~130 KiB/s, ~550 spans/s), but only ~100 spans/s reach Jaeger, and
   collector memory grows. In Jaeger every error trace is there (*Min
   Duration* `500ms` shows the slow ones).
5. Adaptive (E):

   ```bash
   docker compose $ENV --env-file scenarios/E.env --profile load up -d --wait
   ```

   At 30 users ~270 spans/s (~14 traces/s, cap 15) reach Jaeger; set Locust
   to 10 users and every trace is kept (~180 spans/s).
6. Stop: `docker compose --profile load down`.

## Repository layout

```
services/              # gateway, orders, payments, inventory (FastAPI), adaptive_sampler/
collector/configs/     # OTel Collector configs: passthrough, tail
jaeger/, monitoring/   # Jaeger, Prometheus and Grafana config
load/locustfile.py     # steady load: 1 request/s per user
scenarios/             # A-E env files + faults.env
scripts/               # verify_*.py checks, run_experiment.py
analysis/analyze.py    # CSV -> summary tables and charts (--lang en/uk)
results/raw/runs.csv   # final dataset (30 runs)
results/analysis/      # summary tables and charts (uk/: Ukrainian charts)
docs/                  # results, experiment and stand details
docker-compose.yml
```
