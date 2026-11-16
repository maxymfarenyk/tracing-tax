"""Fault injection shared by all services.

Configured via environment variables (read once at import time):
    ERROR_RATE     probability 0..1 that a request fails with HTTP 500 (default 0)
    SLOW_RATE      probability 0..1 that a request gets extra delay (default 0)
    SLOW_DELAY_MS  extra delay in milliseconds for slow requests (default 500)
"""

import asyncio
import os
import random

from fastapi import HTTPException


def _rate(name: str) -> float:
    value = float(os.getenv(name, "0"))
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {value}")
    return value


ERROR_RATE = _rate("ERROR_RATE")
SLOW_RATE = _rate("SLOW_RATE")
SLOW_DELAY_S = float(os.getenv("SLOW_DELAY_MS", "500")) / 1000.0


async def inject_faults() -> None:
    """FastAPI dependency: maybe add delay, then maybe fail the request.

    The delay is applied before the error so that a request can be both slow
    and failed, which keeps the two fault types independent.
    """
    if SLOW_RATE and random.random() < SLOW_RATE:
        await asyncio.sleep(SLOW_DELAY_S)
    if ERROR_RATE and random.random() < ERROR_RATE:
        raise HTTPException(status_code=500, detail="injected fault")
