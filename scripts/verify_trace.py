"""End-to-end smoke test for the tracing pipeline.

Sends one plain request (no trace headers, like a curl) to the gateway with a
unique order id, finds the resulting trace via the Jaeger v2 query API
(/api/v3), and checks that it contains spans from both `gateway` and `orders`
linked by correct parent-child relationships.

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


def _id(raw: str) -> str:
    """Normalise span/trace IDs to lowercase hex (Jaeger may return hex or base64)."""
    if raw and all(c in "0123456789abcdefABCDEF" for c in raw):
        return raw.lower()
    return base64.b64decode(raw).hex()


# OTLP SpanKind enum; Jaeger's /api/v3 returns it as an integer.
_KINDS = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}


def _kind(raw) -> str:
    if isinstance(raw, int):
        return _KINDS.get(raw, str(raw))
    return str(raw).removeprefix("SPAN_KIND_")


def _attr_values(attrs: list[dict]) -> list[str]:
    return [str(next(iter(a["value"].values()), "")) for a in attrs or []]


def _flatten(body: dict) -> list[dict]:
    spans = []
    for rs in body.get("result", {}).get("resourceSpans", []):
        service = next(
            (a["value"]["stringValue"] for a in rs["resource"]["attributes"] if a["key"] == "service.name"),
            "?",
        )
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
                        "attrs": _attr_values(s.get("attributes")),
                    }
                )
    return spans


def _get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # nothing found (yet)
            return {}
        raise


def find_trace(jaeger: str, path: str, since: datetime, timeout_s: float) -> list[dict]:
    """Poll Jaeger until a trace containing `path` has spans from both services."""
    deadline = time.monotonic() + timeout_s
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
        trace_ids = {s["trace_id"] for s in spans if any(path in v for v in s["attrs"])}
        if len(trace_ids) == 1:
            trace = _flatten(_get(f"{jaeger}/api/v3/traces/{trace_ids.pop()}"))
            # Spans of one trace arrive in separate export batches; wait for both.
            if {"gateway", "orders"} <= {s["service"] for s in trace}:
                return trace
        elif len(trace_ids) > 1:
            sys.exit(f"FAIL: expected one trace for {path}, found {len(trace_ids)}")
        if time.monotonic() > deadline:
            sys.exit(f"FAIL: no complete trace for {path} in Jaeger after {timeout_s:.0f}s")
        time.sleep(1)


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
        print(f"GET {path} -> HTTP {resp.status}: {resp.read().decode()}")

    spans = find_trace(args.jaeger, path, since, args.timeout)
    trace_id = spans[0]["trace_id"]
    by_id = {s["span_id"]: s for s in spans}

    print(f"\ntrace {trace_id}: {len(spans)} spans")
    for s in sorted(spans, key=lambda s: s["start"]):
        parent = by_id.get(s["parent_id"])
        parent_desc = f"{parent['service']}:{parent['name']}" if parent else "<root>"
        print(f"  {s['service']:8} {s['kind']:8} {s['name']:34} {(s['end'] - s['start']) / 1e6:7.1f} ms  parent={parent_desc}")

    def one(service: str, kind: str) -> dict:
        matches = [s for s in spans if s["service"] == service and s["kind"] == kind]
        if len(matches) != 1:
            sys.exit(f"FAIL: expected 1 {kind} span from {service}, got {len(matches)}")
        return matches[0]

    gw_server = one("gateway", "SERVER")
    gw_client = one("gateway", "CLIENT")
    orders_server = one("orders", "SERVER")

    checks = {
        "gateway SERVER span is the trace root": gw_server["parent_id"] is None,
        "gateway CLIENT span is a child of gateway SERVER span": gw_client["parent_id"] == gw_server["span_id"],
        "orders SERVER span is a child of gateway CLIENT span": orders_server["parent_id"] == gw_client["span_id"],
        "every non-root span has its parent inside the trace": all(
            s["parent_id"] in by_id for s in spans if s["parent_id"] is not None
        ),
    }
    print()
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not all(checks.values()):
        sys.exit(1)
    print(f"\nOK. Open in Jaeger UI: {args.jaeger}/trace/{trace_id}")


if __name__ == "__main__":
    main()
