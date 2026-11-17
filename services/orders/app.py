"""orders service: builds an order, reserves stock in `inventory`, then
charges it in `payments` (sequentially, like a real checkout).

Tracing is added externally by `opentelemetry-instrument`; this module has no
OpenTelemetry code on purpose.
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

import downstream
from faults import inject_faults

WORK_S = float(os.getenv("WORK_MS", "20")) / 1000.0
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")
PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://payments:8000")

ITEMS = [{"product": "book", "qty": 1, "price": 10.0}, {"product": "pen", "qty": 2, "price": 5.5}]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with downstream.client(INVENTORY_URL) as inventory, downstream.client(PAYMENTS_URL) as payments:
        app.state.inventory = inventory
        app.state.payments = payments
        yield


app = FastAPI(title="orders", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/orders/{order_id}", dependencies=[Depends(inject_faults)])
async def get_order(order_id: int) -> dict:
    await asyncio.sleep(WORK_S)  # simulated business logic
    reservation = await downstream.call(
        app.state.inventory,
        "POST",
        "/reservations",
        json={"order_id": order_id, "items": [{"product": i["product"], "qty": i["qty"]} for i in ITEMS]},
    )
    payment = await downstream.call(
        app.state.payments,
        "POST",
        "/payments",
        json={"order_id": order_id, "amount": sum(i["qty"] * i["price"] for i in ITEMS)},
    )
    return {
        "order_id": order_id,
        "status": "confirmed",
        "items": ITEMS,
        "reservation": reservation,
        "payment": payment,
    }
