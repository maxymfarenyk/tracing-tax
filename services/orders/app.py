"""orders service: returns an order after simulating a short unit of work.

Tracing is added externally by `opentelemetry-instrument`; this module has no
OpenTelemetry code on purpose.
"""

import asyncio
import os

from fastapi import Depends, FastAPI

from faults import inject_faults

WORK_S = float(os.getenv("WORK_MS", "20")) / 1000.0

app = FastAPI(title="orders")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/orders/{order_id}", dependencies=[Depends(inject_faults)])
async def get_order(order_id: int) -> dict:
    await asyncio.sleep(WORK_S)  # simulated business logic / DB access
    return {
        "order_id": order_id,
        "status": "confirmed",
        "items": [{"sku": "SKU-1", "qty": 1}, {"sku": "SKU-2", "qty": 2}],
    }
