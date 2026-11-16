"""gateway service: public entry point, proxies order lookups to `orders`.

Tracing (including W3C traceparent propagation on the outgoing httpx call) is
added externally by `opentelemetry-instrument`; this module has no
OpenTelemetry code on purpose.
"""

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException

from faults import inject_faults

ORDERS_URL = os.getenv("ORDERS_URL", "http://orders:8000")
TIMEOUT_S = float(os.getenv("DOWNSTREAM_TIMEOUT_S", "5"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One pooled client per process; created after auto-instrumentation has
    # patched httpx, so its requests get client spans.
    async with httpx.AsyncClient(base_url=ORDERS_URL, timeout=TIMEOUT_S) as client:
        app.state.orders = client
        yield


app = FastAPI(title="gateway", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/order/{order_id}", dependencies=[Depends(inject_faults)])
async def get_order(order_id: int) -> dict:
    try:
        resp = await app.state.orders.get(f"/orders/{order_id}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"orders unreachable: {exc}")
    if resp.status_code >= 500:
        # Propagate downstream failure so the root span is marked as an error.
        raise HTTPException(status_code=502, detail="orders failed")
    resp.raise_for_status()
    return {"source": "gateway", "order": resp.json()}
