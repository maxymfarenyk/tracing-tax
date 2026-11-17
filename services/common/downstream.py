"""HTTP calls to downstream services with uniform error mapping.

Any downstream failure (connection error, timeout or 5xx) becomes HTTP 502 in
the caller, so the error propagates up to the root span of the trace.
"""

import os

import httpx
from fastapi import HTTPException

TIMEOUT_S = float(os.getenv("DOWNSTREAM_TIMEOUT_S", "5"))


def client(base_url: str) -> httpx.AsyncClient:
    """Create a pooled client. Call it after startup (e.g. in the FastAPI
    lifespan), when auto-instrumentation has already patched httpx."""
    return httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT_S)


async def call(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    name = client.base_url.host
    try:
        resp = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"{name} unreachable: {exc!r}")
    if resp.status_code >= 500:
        raise HTTPException(status_code=502, detail=f"{name} failed with {resp.status_code}")
    resp.raise_for_status()
    return resp.json()
