"""payments service: simulates charging an order (no real payment provider).

Tracing is added externally by `opentelemetry-instrument`; this module has no
OpenTelemetry code on purpose.
"""

import asyncio
import os
import uuid

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from faults import inject_faults

WORK_S = float(os.getenv("WORK_MS", "30")) / 1000.0

app = FastAPI(title="payments")


class Payment(BaseModel):
    order_id: int
    amount: float


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/payments", dependencies=[Depends(inject_faults)])
async def create_payment(payment: Payment) -> dict:
    await asyncio.sleep(WORK_S)  # simulated call to a payment provider
    return {
        "payment_id": str(uuid.uuid4()),
        "order_id": payment.order_id,
        "amount": payment.amount,
        "status": "captured",
    }
