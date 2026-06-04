"""Rate-limit middleware unit test (Security Sprint H2)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.middleware.ratelimit import RateLimitMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, per_minute=3, webhook_per_minute=3)

    @app.get("/api/thing")
    async def thing():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


@pytest.mark.asyncio
async def test_rate_limit_blocks_after_threshold():
    transport = httpx.ASGITransport(app=_build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        # 3 allowed, 4th blocked (per_minute=3).
        codes = [(await ac.get("/api/thing")).status_code for _ in range(4)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429


@pytest.mark.asyncio
async def test_health_is_exempt():
    transport = httpx.ASGITransport(app=_build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        codes = [(await ac.get("/health")).status_code for _ in range(10)]
    assert all(c == 200 for c in codes)
