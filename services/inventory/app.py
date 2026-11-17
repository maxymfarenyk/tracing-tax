"""inventory service: reserves products for an order in Redis.

Each reserved item is one Redis INCRBY, so a reservation produces real
database client spans. Tracing is added externally by `opentelemetry-instrument`;
this module has no OpenTelemetry code on purpose.
"""

import os
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import Depends, FastAPI
from pydantic import BaseModel

from faults import inject_faults

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


class Item(BaseModel):
    product: str
    qty: int


class Reservation(BaseModel):
    order_id: int
    items: list[Item]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Created after auto-instrumentation has patched the redis client.
    app.state.redis = redis.Redis.from_url(REDIS_URL)
    try:
        yield
    finally:
        await app.state.redis.aclose()


app = FastAPI(title="inventory", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    # No Redis ping here: excluding /health from tracing only suppresses the
    # HTTP span, a PING would still create a standalone trace on every probe.
    # Redis readiness is handled by its own healthcheck in docker-compose.yml.
    return {"status": "ok"}


@app.post("/reservations", dependencies=[Depends(inject_faults)])
async def reserve(reservation: Reservation) -> dict:
    reserved = {}
    for item in reservation.items:
        reserved[item.product] = await app.state.redis.incrby(f"reserved:{item.product}", item.qty)
    return {"order_id": reservation.order_id, "reserved_total": reserved}
