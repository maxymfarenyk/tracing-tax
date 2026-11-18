"""Check that a scenario's sampling keeps the expected share of traces.

Reads the expected ratio from a scenario env file (scenarios/<X>.env), sends N
requests to the gateway and compares the number of spans the collector stored
in Jaeger (Prometheus counter, before/after delta) with N x ratio:

    A (SDK disabled)              -> no spans at all
    B (always_on)                 -> every trace
    C (parentbased_traceidratio)  -> ~ratio of traces, within 3 sigma
    D (COLLECTOR_CONFIG=tail)     -> the collector still receives every span
                                     (services export 100%), stores ~10%:
                                     without faults only the probabilistic
                                     policy of collector/configs/tail.yaml fires
    E (adaptive)                  -> every trace: sequential requests stay
                                     below the cap (OTEL_TRACES_SAMPLER_ARG)

Deltas are used because Jaeger's in-memory store keeps traces from earlier
scenarios until it restarts. Run it with the stack started from the same file:

    docker compose --env-file .env --env-file scenarios/C.env up -d --wait
    python3 scripts/verify_sampling.py scenarios/C.env

Stdlib only; assumes no other traffic while it runs.
"""

import argparse
import math
import sys
import time
import urllib.request
from pathlib import Path

from verify_monitoring import ACCEPTED, FRESH, SPANS_PER_REQUEST, STORED, query

# Probabilistic policy of collector/configs/tail.yaml (sampling_percentage).
TAIL_BASELINE_RATIO = 0.1


def read_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def expected_ratio(env: dict[str, str]) -> float:
    if env.get("OTEL_SDK_DISABLED", "false").lower() == "true":
        return 0.0
    sampler = env.get("OTEL_TRACES_SAMPLER", "parentbased_always_on")
    if sampler in ("always_on", "parentbased_always_on"):
        return 1.0
    if sampler in ("always_off", "parentbased_always_off"):
        return 0.0
    if sampler in ("traceidratio", "parentbased_traceidratio"):
        return float(env.get("OTEL_TRACES_SAMPLER_ARG", "1.0"))
    if sampler == "adaptive":
        # Requests are sent one at a time (<= ~10/s), below the cap.
        return 1.0
    sys.exit(f"FAIL: don't know the expected ratio of sampler {sampler!r}")


def span_counter(prom: str, sel: str, timeout_s: float) -> float:
    """Span counter, or 0 if the collector has never received a span.

    The collector only creates the counter on the first span, so in scenario A
    it may not exist at all. Missing is only trusted if the collector is up.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if result := query(prom, FRESH.format(sel=sel)):
            return float(result[0]["value"][1])
        if query(prom, 'last_over_time(up{job="otel-collector"}[10s]) == 1'):
            return 0.0
        if time.monotonic() > deadline:
            sys.exit("FAIL: collector is not being scraped by Prometheus")
        time.sleep(1)


def settle(prom: str, timeout_s: float, quiet_s: float = 12.0) -> tuple[float, float]:
    """Wait until the stored-span counter stops changing (see verify_monitoring).

    Returns (accepted, stored). Stored spans come last (after the tail sampling
    decision, if any), so once they settle the accepted counter has too.
    """
    deadline = time.monotonic() + timeout_s
    last, since = None, time.monotonic()
    while True:
        now = span_counter(prom, STORED, timeout_s)
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet_s:
            return span_counter(prom, ACCEPTED, timeout_s), now
        if time.monotonic() > deadline:
            sys.exit("FAIL: span counter never settled; is other traffic hitting the gateway?")
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sampling ratio check.")
    ap.add_argument("scenario", type=Path, help="scenario env file, e.g. scenarios/C.env")
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--prometheus", default="http://localhost:9090")
    ap.add_argument("--requests", type=int, default=500)
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()
    prom, n = args.prometheus, args.requests

    env = read_env(args.scenario)
    tail = env.get("COLLECTOR_CONFIG") == "tail"
    exported = expected_ratio(env)
    ratio = TAIL_BASELINE_RATIO if tail else exported
    print(f"scenario {args.scenario.stem}: expected ratio {ratio:g}" + (" (tail sampling)" if tail else ""))

    print("waiting for span counter to settle ...")
    accepted_before, before = settle(prom, args.timeout)

    print(f"sending {n} requests to {args.gateway} ...")
    for i in range(n):
        with urllib.request.urlopen(f"{args.gateway}/order/{i}", timeout=10) as resp:
            if resp.status != 200:
                sys.exit(f"FAIL: request {i} returned HTTP {resp.status}")

    print("waiting for new spans to reach Prometheus ...")
    accepted_after, after = settle(prom, args.timeout)
    spans = int(after - before)
    accepted = int(accepted_after - accepted_before)
    traces = spans / SPANS_PER_REQUEST
    observed = traces / n
    # Binomial: each root decision is independent with probability `ratio`.
    tolerance = 3 * math.sqrt(ratio * (1 - ratio) / n)

    print(f"\n{spans} spans stored = {traces:g} traces of {n} requests (ratio {observed:.3f})\n")
    checks = {
        f"whole traces only ({spans} spans is a multiple of {SPANS_PER_REQUEST})": spans % SPANS_PER_REQUEST == 0,
        f"ratio {observed:.3f} within {ratio:g} ± {tolerance:.3f}": abs(observed - ratio) <= tolerance,
    }
    if tail:
        expected = round(n * exported * SPANS_PER_REQUEST)
        checks[f"services exported every span ({accepted} accepted = {expected})"] = accepted == expected
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
