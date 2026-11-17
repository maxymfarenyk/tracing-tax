"""gateway service: public entry point, proxies order lookups to `orders`.

Tracing (including W3C traceparent propagation on the outgoing httpx call) is
added externally by `opentelemetry-instrument`; this module has no
OpenTelemetry code on purpose.
"""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

import downstream
from faults import inject_faults

ORDERS_URL = os.getenv("ORDERS_URL", "http://orders:8000")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with downstream.client(ORDERS_URL) as orders:
        app.state.orders = orders
        yield


app = FastAPI(title="gateway", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/order/{order_id}", dependencies=[Depends(inject_faults)])
async def get_order(order_id: int) -> dict:
    order = await downstream.call(app.state.orders, "GET", f"/orders/{order_id}")
    return {"source": "gateway", "order": order}
