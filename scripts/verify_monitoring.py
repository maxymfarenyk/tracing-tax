"""Smoke test for the monitoring stack (Prometheus + cAdvisor + collector metrics).

Sends a fixed number of requests to the gateway, then checks via the
Prometheus HTTP API that:
  - every scrape target is up,
  - cAdvisor reports CPU, memory and network for every compose service,
  - the collector accepted, and sent to Jaeger, exactly requests x spans-per-request
    spans (proves the span counters can be trusted for the experiments).

Assumes 100% sampling (the default) and no other traffic while it runs.
Stdlib only:
    python3 scripts/verify_monitoring.py
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

PROJECT = "tracing-tax"
SERVICES = [
    "gateway", "orders", "inventory", "payments", "redis",
    "otel-collector", "jaeger", "prometheus", "grafana", "cadvisor",
]
TARGETS = {"cadvisor", "otel-collector"}

# Spans per successful request with the current topology: 4 SERVER, 3 HTTP
# CLIENT, 2 Redis CLIENT, 10 ASGI INTERNAL (see `scripts/verify_trace.py`).
SPANS_PER_REQUEST = 19

# Counters are read only from samples of the last 10 s. Right after a restart
# of the whole stack (including Prometheus) the previous run's last value is
# still "current" for up to 5 minutes, because Prometheus never saw the old
# series end, so a plain instant query would return a stale baseline.
ACCEPTED = 'otelcol_receiver_accepted_spans{job="otel-collector"}'
# Spans successfully exported to Jaeger, i.e. stored.
STORED = 'otelcol_exporter_sent_spans{job="otel-collector"}'
FRESH = "sum(last_over_time({sel}[10s]))"


def query(prom: str, expr: str) -> list[dict]:
    url = f"{prom}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=5) as resp:
        body = json.load(resp)
    if body.get("status") != "success":
        sys.exit(f"FAIL: Prometheus query failed: {expr}: {body}")
    return body["data"]["result"]


def scalar(prom: str, expr: str) -> float:
    result = query(prom, expr)
    return float(result[0]["value"][1]) if result else 0.0


def counter(prom: str, selector: str, timeout_s: float) -> float:
    """Current value of a counter, waiting until it has a fresh sample."""
    deadline = time.monotonic() + timeout_s
    while not (result := query(prom, FRESH.format(sel=selector))):
        if time.monotonic() > deadline:
            sys.exit(f"FAIL: no fresh samples of {selector} in Prometheus")
        time.sleep(1)
    return float(result[0]["value"][1])


def settle(prom: str, timeout_s: float, quiet_s: float = 12.0) -> tuple[float, float]:
    """Wait until both span counters stop changing and return them.

    Spans from earlier traffic keep arriving for a while: the Python SDK
    exports every 5 s (OTEL_BSP_SCHEDULE_DELAY) and Prometheus scrapes every
    5 s. `quiet_s` covers both, so the baseline excludes earlier requests.
    """
    deadline = time.monotonic() + timeout_s
    last, since = None, time.monotonic()
    while True:
        now = (counter(prom, ACCEPTED, timeout_s), counter(prom, STORED, timeout_s))
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet_s:
            return now
        if time.monotonic() > deadline:
            sys.exit("FAIL: span counters never settled; is other traffic hitting the gateway?")
        time.sleep(1)


def per_service(prom: str, expr: str) -> set[str]:
    return {r["metric"].get("service", "") for r in query(prom, expr)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Monitoring smoke test.")
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--prometheus", default="http://localhost:9090")
    ap.add_argument("--grafana", default="http://localhost:3000")
    ap.add_argument("--requests", type=int, default=50)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()
    prom = args.prometheus

    print("waiting for span counters to settle ...")
    accepted_before, stored_before = settle(prom, args.timeout)

    print(f"sending {args.requests} requests to {args.gateway} ...")
    for i in range(args.requests):
        with urllib.request.urlopen(f"{args.gateway}/order/{i}", timeout=10) as resp:
            if resp.status != 200:
                sys.exit(f"FAIL: request {i} returned HTTP {resp.status}")

    expected = args.requests * SPANS_PER_REQUEST
    print(f"waiting for {expected} new spans to reach Prometheus ...")
    accepted_after, stored_after = settle(prom, args.timeout)
    accepted = accepted_after - accepted_before
    stored = stored_after - stored_before

    label = 'label_replace({expr}, "service", "$1", "container_label_com_docker_compose_service", "(.+)")'
    sel = f'{{container_label_com_docker_compose_project="{PROJECT}"}}'
    series = {
        "CPU": per_service(prom, label.format(expr=f"rate(container_cpu_usage_seconds_total{sel}[1m])")),
        "memory": per_service(prom, label.format(expr=f"container_memory_working_set_bytes{sel}")),
        "network": per_service(prom, label.format(expr=f"rate(container_network_receive_bytes_total{sel}[1m])")),
    }
    targets = query(prom, "up")
    up = {r["metric"]["job"] for r in targets if r["value"][1] == "1"}
    collector_rx = scalar(
        prom,
        f'sum(rate(container_network_receive_bytes_total{{container_label_com_docker_compose_project="{PROJECT}",'
        f' container_label_com_docker_compose_service="otel-collector"}}[1m]))',
    )

    checks = {f"scrape target up: {t}": t in up for t in sorted(TARGETS)}
    for kind, found in series.items():
        missing = sorted(set(SERVICES) - found)
        checks[f"cAdvisor {kind} series for all {len(SERVICES)} services" + (f" (missing: {missing})" if missing else "")] = (
            not missing
        )
    checks[f"collector network RX > 0 ({collector_rx:.0f} B/s)"] = collector_rx > 0
    checks[f"collector accepted {accepted:.0f} spans == {expected} expected"] = accepted == expected
    checks[f"collector sent {stored:.0f} spans to Jaeger == {expected} expected"] = stored == expected

    print()
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not all(checks.values()):
        sys.exit(1)
    print(f"\nOK. Dashboard: {args.grafana}/d/tracing-tax")


if __name__ == "__main__":
    main()
