# Experiment and analysis

How `scripts/run_experiment.py` and `analysis/analyze.py` work. The
commands to run them are in the [README](../README.md#experiment); the
results are in [results.md](results.md).

## Experiment

`scripts/run_experiment.py` runs every scenario at both load levels, three
times, and writes one CSV row per run to `results/raw/runs.csv`. It needs only
Docker and the host Python (stdlib only).

```bash
# Pilot: 2 scenarios, 1 load level, 1 repetition (~8 min)
python3 scripts/run_experiment.py --scenarios A,B --levels high --reps 1 --out results/raw/pilot.csv

# Full experiment: all scenarios/[A-Z].env x low, high x 3 repetitions
python3 scripts/run_experiment.py
```

Each run:

1. **Restart.** Recreates the measured containers (the four services, Redis, the
   collector and Jaeger), so every run starts with fresh processes, empty
   Jaeger storage and zeroed collector counters. Prometheus, cAdvisor and
   Grafana keep running.
2. **Load.** One Locust run: a 30 s warm-up (cold processes, connection pools,
   the first span batches; discarded), then 2 min measured.
3. **Drain.** Waits 20 s, so the last spans reach Jaeger and the last metrics
   reach Prometheus. With tail sampling (D) the collector holds every trace for
   `decision_wait` (10 s) before it exports it, so D waits 25 s and reads
   `spans_stored` 10 s later than the other scenarios.
4. **Collect.** Reads the Locust request log, Prometheus and Jaeger, then appends
   the row to the CSV.

Runs are interleaved (repetition 1 of every scenario and level, then repetition 2,
and so on), so slow drift of the host affects all scenarios alike. Runs that are already
in the CSV are skipped, so after a crash or `Ctrl+C` run the same command again
and it continues where it stopped. One run takes about 3.5 minutes, so A–D
(24 runs) take about 1 h 25 min and A–E (30 runs) about 1 h 45 min. See `--help` for
the other options (`--warmup`, `--duration`, `--drain`, `--no-build`, ...).

**Host.** Keep the machine otherwise idle during the experiment. On a laptop,
keep it on AC power: on battery the CPU runs slower, which in a test run
raised service CPU by ~20% and p50 latency by ~80 ms. A run measured under
different host conditions is not comparable; delete its row from the CSV and
run the same command again to repeat it.

**Faults.** Every compose call gets `.env` (if present), `scenarios/faults.env`
and the scenario file, so fault injection is identical in all scenarios:
`payments` fails 5% of requests and delays another, independent 5% by 500 ms.
Without faults there would be nothing to measure in the error/slow retention columns.

**Counting kept traces.** Locust sends every request with a unique order ID and
logs it (`results/raw/locust/<run_id>.json`, gitignored). The runner looks up
each request from the measured window in Jaeger by that ID (it is part of the
root span's URL). Error and slow are decided once, from what the client saw:
an error is a failed response (5xx), and slow means at least 500 ms. So
"kept" never exceeds "sent". Requests started in the last 5 s of the window
are not counted, because they may still be in flight when Locust stops.

| Columns | Source | Meaning |
|---|---|---|
| `run_id`, `scenario`, `level`, `users`, `rep`, `started_at`, `warmup_s`, `duration_s` | runner | run identity and settings |
| `measure_start`, `measure_end` | Locust | measured window (Unix time) |
| `requests`, `rps`, `p50_ms`, `p95_ms`, `p99_ms` | Locust log | requests in the window, their rate and exact latency percentiles |
| `errors_sent`, `slow_sent` | Locust log | failed / slow (≥ 500 ms) requests among them |
| `cpu_<container>`, `cpu_services` | cAdvisor | mean CPU in cores over the window; `services` = sum of the four services |
| `mem_<container>_mb`, `mem_services_mb` | cAdvisor | mean memory working set, MiB |
| `collector_rx_bytes` | cAdvisor | bytes received by the collector container in the window |
| `spans_accepted`, `spans_stored` | collector | spans received from the services / written to Jaeger |
| `traces_kept`, `errors_kept`, `slow_kept` | Jaeger | sent requests whose trace is in Jaeger |
| `errors_kept_share`, `slow_kept_share` | derived | `errors_kept / errors_sent`, `slow_kept / slow_sent` |

Containers are `gateway`, `orders`, `payments`, `inventory`, `redis`, `collector`
and `jaeger`.

## Analysis

`analysis/analyze.py` turns the experiment CSV into the summary table and
charts for the report. It needs uv (pandas and matplotlib are pinned in the
root `pyproject.toml`).

```bash
uv run analysis/analyze.py                          # results/raw/runs.csv
uv run analysis/analyze.py results/raw/pilot.csv    # any CSV(s) from the runner
```

Several CSVs are merged (run IDs must not repeat). Every metric is aggregated
per scenario and load level as mean ± sample SD over the repetitions; with a
single repetition only the mean is shown. Output goes to `results/analysis/`
(`--out` to change it) and is overwritten on every run:

| File | Content |
|---|---|
| `summary.md` | one Markdown table for the report (RPS, latency, CPU, RAM, network, spans, kept shares) |
| `summary.csv` | the same numbers in long form: `scenario, level, metric, mean, sd, n` |
| `cpu_services.png` | CPU of the four services, cores |
| `latency.png` | p95 and p99 latency, ms |
| `collector_network.png` | traffic into the collector, KiB/s (`collector_rx_bytes / duration_s`) |
| `collector_memory.png` | collector working set, MiB |
| `traces_kept.png` | share of error and slow traces kept, % |

All charts are grouped bars: one group per scenario, one bar per load level,
error bars = SD.
