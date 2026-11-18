"""Experiment runner: scenarios x load levels x repetitions -> one CSV row per run.

Per run:
    1. recreate the measured stack (services, Redis, collector, Jaeger), so
       every run starts from an empty Jaeger, fresh collector counters and
       fresh processes;
    2. one Locust run: warm-up (stats discarded), then the measured window;
    3. wait for the last spans and metrics to arrive (drain; longer for
       tail sampling, whose decisions come `decision_wait` later);
    4. collect the Locust request log, Prometheus metrics (CPU/RAM, bytes into
       the collector, span counters) and the traces kept in Jaeger;
    5. append the row to the CSV.

Runs already in the CSV are skipped, so after a crash or Ctrl+C just run the
same command again. Every compose call gets the same env files
(.env if present, scenarios/faults.env, scenarios/<X>.env), so faults are
identical in every scenario.

Sampling shares are computed per request: every request Locust started in
[measure_start, measure_end - END_GRACE_S) is looked up in Jaeger by its unique
order id (in the root span's URL). Error/slow is decided once, from what the
client saw, so "kept" can never exceed "sent". Resource metrics cover the
whole measured window.

Stdlib only:
    python3 scripts/run_experiment.py --scenarios A,B --levels high --reps 1 \\
        --out results/raw/pilot.csv      # pilot, ~7 min
    python3 scripts/run_experiment.py    # full experiment
"""

import argparse
import csv
import json
import math
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from verify_monitoring import PROJECT, query
from verify_sampling import read_env
from verify_trace import _flatten, _get

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "scenarios"
FAULTS_ENV = SCENARIOS_DIR / "faults.env"
LOCUST_DIR = "raw/locust"  # under results/, mounted at /mnt/results in Locust

# Users = RPS (see load/locustfile.py).
LEVELS = {"low": 10, "high": 30}

# Recreated before every run. Prometheus, cAdvisor and Grafana keep running.
MEASURED = ["gateway", "orders", "payments", "inventory", "redis", "otel-collector", "jaeger"]
SERVICES = ["gateway", "orders", "payments", "inventory"]
# Compose service -> CSV column suffix.
CONTAINERS = {s: s for s in SERVICES} | {"redis": "redis", "otel-collector": "collector", "jaeger": "jaeger"}

# Span counters are read this long after the window, to include spans that
# were still being exported (SDK batches every 5 s, Prometheus scrapes every 5 s).
EXPORT_LAG_S = 10
# With tail sampling the collector holds every trace for `decision_wait` before
# exporting it, so stored spans lag the accepted ones by that much. Must match
# collector/configs/tail.yaml.
TAIL_DECISION_WAIT_S = 10
# Jaeger is searched in slices of this length; a slice must hold fewer traces
# than SEARCH_DEPTH, otherwise the count would be silently truncated.
SLICE_S = 10
SEARCH_DEPTH = 2000
# Requests started in the last seconds of the window may still be in flight
# when Locust stops (and are then never logged), so they are not counted.
# Equals the services' downstream timeout (DOWNSTREAM_TIMEOUT_S).
END_GRACE_S = 5
ORDER_ID = re.compile(r"/order/(\d+)")

COLUMNS = (
    ["run_id", "scenario", "level", "users", "rep", "started_at", "warmup_s", "duration_s"]
    + ["measure_start", "measure_end"]
    + ["requests", "rps", "p50_ms", "p95_ms", "p99_ms", "errors_sent", "slow_sent"]
    + [f"cpu_{c}" for c in CONTAINERS.values()]
    + ["cpu_services"]
    + [f"mem_{c}_mb" for c in CONTAINERS.values()]
    + ["mem_services_mb"]
    + ["collector_rx_bytes", "spans_accepted", "spans_stored"]
    + ["traces_kept", "errors_kept", "slow_kept", "errors_kept_share", "slow_kept_share"]
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --- Docker Compose ------------------------------------------------------------


def compose(scenario: str, *args: str) -> None:
    env_files = [ROOT / ".env"] if (ROOT / ".env").exists() else []
    env_files += [FAULTS_ENV, SCENARIOS_DIR / f"{scenario}.env"]
    cmd = ["docker", "compose"]
    for f in env_files:
        cmd += ["--env-file", str(f)]
    subprocess.run(cmd + list(args), cwd=ROOT, check=True)


def run_load(scenario: str, run_id: str, users: int, args: argparse.Namespace) -> None:
    # --exit-code-on-error 0: injected faults are expected, not a failed run.
    compose(
        scenario, "run", "--rm", "locust",
        "--headless", "--users", str(users), "--spawn-rate", str(users),
        "--run-time", f"{args.warmup + args.duration}s",
        "--only-summary", "--exit-code-on-error", "0",
        "--warmup-s", str(args.warmup),
        "--requests-json", f"/mnt/results/{LOCUST_DIR}/{run_id}.json",
    )  # fmt: skip


# --- Locust --------------------------------------------------------------------


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile."""
    return sorted_values[max(0, math.ceil(q / 100 * len(sorted_values)) - 1)]


def load_requests(run_id: str, slow_ms: float) -> tuple[dict, dict[int, tuple[bool, bool]]]:
    """Locust metrics of the measured window, and {order id: (error, slow)}."""
    log_ = json.loads((ROOT / "results" / LOCUST_DIR / f"{run_id}.json").read_text())
    t_start, t_end = log_["measure_start"], log_["measure_end"]
    count_end = t_end - END_GRACE_S
    reqs = [r for r in log_["requests"] if t_start <= r[0] < count_end]
    if not reqs:
        raise RuntimeError(f"{run_id}: Locust sent no requests in the measured window")
    sent = {oid: (bool(failed), ms >= slow_ms) for _, ms, failed, oid in reqs}
    latencies = sorted(r[1] for r in reqs)
    row = {
        "measure_start": t_start,
        "measure_end": t_end,
        "requests": len(reqs),
        "rps": round(len(reqs) / (count_end - t_start), 2),
        "p50_ms": round(percentile(latencies, 50), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "p99_ms": round(percentile(latencies, 99), 1),
        "errors_sent": sum(e for e, _ in sent.values()),
        "slow_sent": sum(s for _, s in sent.values()),
    }
    return row, sent


# --- Prometheus ----------------------------------------------------------------


def by_service(prom: str, expr: str, at: float) -> dict[str, float]:
    label = "container_label_com_docker_compose_service"
    return {r["metric"].get(label, ""): float(r["value"][1]) for r in query(prom, expr, at)}


def stored_lag(scenario: str) -> float:
    """Seconds after the window at which all its spans have been exported."""
    env = read_env(SCENARIOS_DIR / f"{scenario}.env")
    extra = TAIL_DECISION_WAIT_S if env.get("COLLECTOR_CONFIG") == "tail" else 0
    return EXPORT_LAG_S + extra


def prometheus_results(prom: str, t_end: float, duration: float, lag_stored: float) -> dict:
    d = f"{round(duration)}s"
    sel = f'{{container_label_com_docker_compose_project="{PROJECT}"}}'
    by = "container_label_com_docker_compose_service"
    # Stale series of the replaced containers add 0 to the CPU sum; for memory
    # `max` avoids counting a stale and a live series twice.
    cpu = by_service(prom, f"sum by ({by}) (increase(container_cpu_usage_seconds_total{sel}[{d}])) / {duration}", t_end)
    mem = by_service(prom, f"max by ({by}) (avg_over_time(container_memory_working_set_bytes{sel}[{d}]))", t_end)
    rx = by_service(prom, f"sum by ({by}) (increase(container_network_receive_bytes_total{sel}[{d}]))", t_end)

    missing = [s for s in CONTAINERS if s not in cpu or s not in mem]
    if missing or "otel-collector" not in rx:
        raise RuntimeError(f"cAdvisor metrics missing for {missing or ['otel-collector network']}")

    # Span counters only exist after the collector's first span (none in A),
    # so missing means 0, but only if the collector was being scraped.
    at = t_end + EXPORT_LAG_S
    if not query(prom, f'max_over_time(up{{job="otel-collector"}}[{d}]) == 1', at):
        raise RuntimeError("collector was not scraped by Prometheus during the run")

    def spans(metric: str, at: float) -> float:
        result = query(prom, f'sum(increase({metric}{{job="otel-collector"}}[{d}]))', at)
        return round(float(result[0]["value"][1])) if result else 0

    row = {f"cpu_{col}": round(cpu[s], 4) for s, col in CONTAINERS.items()}
    row["cpu_services"] = round(sum(cpu[s] for s in SERVICES), 4)
    row |= {f"mem_{col}_mb": round(mem[s] / 2**20, 1) for s, col in CONTAINERS.items()}
    row["mem_services_mb"] = round(sum(mem[s] for s in SERVICES) / 2**20, 1)
    row["collector_rx_bytes"] = round(rx["otel-collector"])
    row["spans_accepted"] = spans("otelcol_receiver_accepted_spans", at)
    row["spans_stored"] = spans("otelcol_exporter_sent_spans", t_end + lag_stored)
    return row


# --- Jaeger --------------------------------------------------------------------


def _rfc3339(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jaeger_results(jaeger: str, t_start: float, t_end: float, sent: dict[int, tuple[bool, bool]]) -> dict:
    """Look up every sent request's trace in Jaeger by its order id."""
    kept_ids: set[int] = set()
    t = float(int(t_start))
    while t < t_end:
        params = urllib.parse.urlencode(
            {
                "query.service_name": "gateway",
                "query.start_time_min": _rfc3339(t),
                "query.start_time_max": _rfc3339(t + SLICE_S),
                "query.search_depth": SEARCH_DEPTH,
            }
        )
        spans = _flatten(_get(f"{jaeger}/api/v3/traces?{params}", timeout_s=60))
        if len({s["trace_id"] for s in spans}) >= SEARCH_DEPTH:
            raise RuntimeError(f"Jaeger slice at {_rfc3339(t)} hit search depth {SEARCH_DEPTH}")
        for s in spans:
            if s["parent_id"] is None and s["service"] == "gateway":
                for value in s["attrs"].values():
                    if m := ORDER_ID.search(value):
                        kept_ids.add(int(m.group(1)))
                        break
        t += SLICE_S

    kept = [sent[oid] for oid in kept_ids if oid in sent]
    return {
        "traces_kept": len(kept),
        "errors_kept": sum(e for e, _ in kept),
        "slow_kept": sum(s for _, s in kept),
    }


# --- Main ----------------------------------------------------------------------


def share(kept: int, sent: int) -> float | str:
    return round(kept / sent, 4) if sent else ""


def run_once(scenario: str, level: str, rep: int, args: argparse.Namespace) -> dict:
    run_id = f"{scenario}_{level}_r{rep}"
    users = LEVELS[level]
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    log(f"{run_id}: recreating the measured stack")
    compose(scenario, "up", "-d", "--wait", "--force-recreate", *MEASURED)

    log(f"{run_id}: {users} users, {args.warmup:g} s warm-up + {args.duration:g} s measured")
    run_load(scenario, run_id, users, args)

    lag = stored_lag(scenario)
    drain = max(args.drain, lag + 5)
    log(f"{run_id}: draining {drain:g} s, then collecting")
    time.sleep(drain)

    row = {
        "run_id": run_id, "scenario": scenario, "level": level, "users": users, "rep": rep,
        "started_at": started_at, "warmup_s": args.warmup, "duration_s": args.duration,
    }  # fmt: skip
    load, sent = load_requests(run_id, args.slow_threshold_ms)
    row |= load
    t_start, t_end = row["measure_start"], row["measure_end"]
    row |= prometheus_results(args.prometheus, t_end, t_end - t_start, lag)
    # Search a bit past the window: the server starts a trace after the client
    # sent the request, and the order ids decide what counts anyway.
    row |= jaeger_results(args.jaeger, t_start, t_end + END_GRACE_S, sent)
    row["errors_kept_share"] = share(row["errors_kept"], row["errors_sent"])
    row["slow_kept_share"] = share(row["slow_kept"], row["slow_sent"])
    row["measure_start"], row["measure_end"] = round(t_start, 3), round(t_end, 3)
    return row


def done_runs(out: Path) -> set[str]:
    if not out.exists():
        return set()
    with open(out, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            sys.exit(f"{out} has different columns than this runner writes; use another --out")
        return {r["run_id"] for r in reader}


def append_row(out: Path, row: dict) -> None:
    new = not out.exists()
    with open(out, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def main() -> None:
    available = sorted(p.stem for p in SCENARIOS_DIR.glob("[A-Z].env"))
    ap = argparse.ArgumentParser(description="Run the sampling experiment and write results to CSV.")
    ap.add_argument("--scenarios", default=",".join(available), help=f"comma-separated (default: {','.join(available)})")
    ap.add_argument("--levels", default=",".join(LEVELS), help="comma-separated, from: " + ", ".join(LEVELS))
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=30, help="seconds of load before measuring")
    ap.add_argument("--duration", type=int, default=120, help="measured seconds")
    ap.add_argument(
        "--drain", type=float, default=20.0,
        help="seconds to wait after load before collecting (raised automatically for tail sampling)",
    )  # fmt: skip
    ap.add_argument("--slow-threshold-ms", type=float, default=500.0)
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "raw" / "runs.csv")
    ap.add_argument("--prometheus", default="http://localhost:9090")
    ap.add_argument("--jaeger", default="http://localhost:16686")
    ap.add_argument("--no-build", action="store_true", help="skip building the service images")
    args = ap.parse_args()

    scenarios = args.scenarios.split(",")
    levels = args.levels.split(",")
    if bad := [s for s in scenarios if s not in available]:
        sys.exit(f"unknown scenario(s) {bad}; available: {available}")
    if bad := [lv for lv in levels if lv not in LEVELS]:
        sys.exit(f"unknown level(s) {bad}; available: {list(LEVELS)}")
    if args.drain < EXPORT_LAG_S + 5:
        sys.exit(f"--drain must be at least {EXPORT_LAG_S + 5} s")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "results" / LOCUST_DIR).mkdir(parents=True, exist_ok=True)
    done = done_runs(args.out)

    # Repetition outermost: scenarios are interleaved, so slow drift of the
    # host (temperature, background load) affects all of them alike.
    plan = [(s, lv, r) for r in range(1, args.reps + 1) for lv in levels for s in scenarios]
    todo = [p for p in plan if f"{p[0]}_{p[1]}_r{p[2]}" not in done]
    # ~40 s to recreate the stack per run.
    total_s = sum(40 + args.warmup + args.duration + max(args.drain, stored_lag(s) + 5) for s, _, _ in todo)
    log(f"{len(plan)} runs planned, {len(plan) - len(todo)} already in {args.out}, "
        f"{len(todo)} to go (~{total_s / 60:.0f} min)")  # fmt: skip
    if not todo:
        return

    if not args.no_build:
        compose(scenarios[0], "build")
    compose(scenarios[0], "up", "-d", "--wait", "prometheus", "cadvisor", "grafana")

    for i, (scenario, level, rep) in enumerate(todo, 1):
        log(f"--- run {i}/{len(todo)} ---")
        row = run_once(scenario, level, rep, args)
        append_row(args.out, row)
        log(
            f"{row['run_id']}: {row['rps']:.1f} RPS, p95 {row['p95_ms']:.0f} ms, "
            f"CPU services {row['cpu_services']:.2f}, spans stored {row['spans_stored']}, "
            f"traces kept {row['traces_kept']}/{row['requests']}, "
            f"errors kept {row['errors_kept']}/{row['errors_sent']}, "
            f"slow kept {row['slow_kept']}/{row['slow_sent']}"
        )
    log(f"done: {args.out}")


if __name__ == "__main__":
    main()
