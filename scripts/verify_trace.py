"""End-to-end smoke test for the tracing pipeline.

Sends one plain request (no trace headers, like a curl) to the gateway with a
unique order id, finds the resulting trace via the Jaeger v2 query API
(/api/v3), and checks that it spans all services with correct parent-child
relationships:

    gateway -> orders -> inventory -> redis
                      -> payments

Assumes 100% sampling (the default). Stdlib only:
    python3 scripts/verify_trace.py
"""

import argparse
import base64
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SERVICES = {"gateway", "orders", "inventory", "payments"}

# (caller service, callee service): the callee's SERVER span must be a child
# of a CLIENT span of the caller, which itself is a child of the caller's
# SERVER span.
CALLS = [("gateway", "orders"), ("orders", "inventory"), ("orders", "payments")]

# Redis commands issued by inventory per request (one INCRBY per order item).
REDIS_SPANS = 2

# OTLP SpanKind enum; Jaeger's /api/v3 returns it as an integer.
_KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}


def _id(raw: str) -> str:
    """Normalise span/trace IDs to lowercase hex (Jaeger may return hex or base64)."""
    if raw and all(c in "0123456789abcdefABCDEF" for c in raw):
        return raw.lower()
    return base64.b64decode(raw).hex()


def _kind(raw) -> str:
    if isinstance(raw, int):
        return _KINDS.get(raw, str(raw))
    return str(raw).removeprefix("SPAN_KIND_")


def _attrs(attrs: list[dict] | None) -> dict[str, str]:
    return {a["key"]: str(next(iter(a["value"].values()), "")) for a in attrs or []}


def _flatten(body: dict) -> list[dict]:
    spans = []
    for rs in body.get("result", {}).get("resourceSpans", []):
        service = _attrs(rs["resource"]["attributes"]).get("service.name", "?")
        for ss in rs.get("scopeSpans", []):
            for s in ss.get("spans", []):
                spans.append(
                    {
                        "trace_id": _id(s["traceId"]),
                        "service": service,
                        "name": s["name"],
                        "kind": _kind(s.get("kind", 0)),
                        "span_id": _id(s["spanId"]),
                        "parent_id": _id(s["parentSpanId"]) if s.get("parentSpanId") else None,
                        "start": int(s["startTimeUnixNano"]),
                        "end": int(s["endTimeUnixNano"]),
                        "attrs": _attrs(s.get("attributes")),
                    }
                )
    return spans


def _is_redis(span: dict) -> bool:
    # Older semconv uses db.system, newer db.system.name.
    a = span["attrs"]
    return span["kind"] == "CLIENT" and "redis" in (a.get("db.system"), a.get("db.system.name"))


def _get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # nothing found (yet)
            return {}
        raise


def _complete(trace: list[dict]) -> bool:
    """Spans of one trace arrive in separate export batches from each service."""
    return SERVICES <= {s["service"] for s in trace} and sum(map(_is_redis, trace)) >= REDIS_SPANS


def find_trace(jaeger: str, path: str, since: datetime, timeout_s: float) -> list[dict]:
    """Poll Jaeger until the trace containing `path` is complete."""
    deadline = time.monotonic() + timeout_s
    trace: list[dict] = []
    while True:
        params = urllib.parse.urlencode(
            {
                "query.service_name": "gateway",
                "query.start_time_min": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "query.start_time_max": (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        )
        spans = _flatten(_get(f"{jaeger}/api/v3/traces?{params}"))
        trace_ids = {s["trace_id"] for s in spans if any(path in v for v in s["attrs"].values())}
        if len(trace_ids) > 1:
            sys.exit(f"FAIL: expected one trace for {path}, found {len(trace_ids)}")
        if trace_ids:
            trace = _flatten(_get(f"{jaeger}/api/v3/traces/{trace_ids.pop()}"))
            if _complete(trace):
                return trace
        if time.monotonic() > deadline:
            seen = sorted({s["service"] for s in trace})
            sys.exit(f"FAIL: no complete trace for {path} after {timeout_s:.0f}s (services seen: {seen})")
        time.sleep(1)


def print_tree(spans: list[dict]) -> None:
    by_parent: dict[str | None, list[dict]] = {}
    ids = {s["span_id"] for s in spans}
    for s in spans:
        by_parent.setdefault(s["parent_id"] if s["parent_id"] in ids else None, []).append(s)

    def walk(parent: str | None, depth: int) -> None:
        for s in sorted(by_parent.get(parent, []), key=lambda s: s["start"]):
            label = f"{'  ' * depth}{s['service']}: {s['name']}"
            print(f"  {label:52} {s['kind']:8} {(s['end'] - s['start']) / 1e6:7.1f} ms")
            walk(s["span_id"], depth + 1)

    walk(None, 0)


def check(spans: list[dict]) -> dict[str, bool]:
    by_id = {s["span_id"]: s for s in spans}

    def servers(service: str) -> list[dict]:
        return [s for s in spans if s["service"] == service and s["kind"] == "SERVER"]

    def parent(span: dict) -> dict | None:
        return by_id.get(span["parent_id"])

    checks: dict[str, bool] = {}
    for service in sorted(SERVICES):
        checks[f"{service}: exactly one SERVER span"] = len(servers(service)) == 1

    roots = [s for s in spans if s["parent_id"] is None]
    checks["single root span: gateway SERVER"] = (
        len(roots) == 1 and roots[0]["service"] == "gateway" and roots[0]["kind"] == "SERVER"
    )

    def called_by(srv: dict, caller: str) -> bool:
        client = parent(srv)
        if not client or client["service"] != caller or client["kind"] != "CLIENT":
            return False
        p = parent(client)
        return p is not None and p["service"] == caller and p["kind"] == "SERVER"

    for caller, callee in CALLS:
        srvs = servers(callee)
        checks[f"{callee} SERVER <- {caller} CLIENT <- {caller} SERVER"] = bool(srvs) and all(
            called_by(s, caller) for s in srvs
        )

    redis = [s for s in spans if _is_redis(s)]
    inventory_server = servers("inventory")
    checks[f"{REDIS_SPANS} redis CLIENT spans, children of inventory SERVER"] = (
        len(redis) == REDIS_SPANS
        and len(inventory_server) == 1
        and all(r["service"] == "inventory" and r["parent_id"] == inventory_server[0]["span_id"] for r in redis)
    )

    checks["every non-root span has its parent inside the trace"] = all(
        s["parent_id"] in by_id for s in spans if s["parent_id"] is not None
    )
    return checks


def main() -> None:
    ap = argparse.ArgumentParser(description="End-to-end tracing smoke test.")
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--jaeger", default="http://localhost:16686")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    order_id = random.randint(10**8, 10**9 - 1)  # unique marker to locate the trace
    path = f"/order/{order_id}"
    since = datetime.now(timezone.utc) - timedelta(seconds=5)

    with urllib.request.urlopen(f"{args.gateway}{path}", timeout=10) as resp:
        print(f"GET {path} -> HTTP {resp.status}")

    spans = find_trace(args.jaeger, path, since, args.timeout)
    trace_id = spans[0]["trace_id"]
    print(f"\ntrace {trace_id}: {len(spans)} spans, {len({s['service'] for s in spans})} services\n")
    print_tree(spans)

    checks = check(spans)
    print()
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not all(checks.values()):
        sys.exit(1)
    print(f"\nOK. Open in Jaeger UI: {args.jaeger}/trace/{trace_id}")


if __name__ == "__main__":
    main()
