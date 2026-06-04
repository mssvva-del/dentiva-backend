"""Lightweight, dependency-free per-IP rate limiting (Security Sprint H2).

In-process fixed-window counter. No external deps (slowapi/redis) — the backend
runs a single uvicorn process on Railway, so an in-memory store is sufficient
for abuse protection. Gated by ``RATE_LIMIT_ENABLED`` (default off) so it never
risks the live demo until enabled for real traffic.

Limits are intentionally generous (a single voice call bursts many webhooks;
the dashboard polls). It only blocks clear abuse, not normal use.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


def _client_ip(request: Request) -> str:
    # Behind Railway's proxy the real client is the first X-Forwarded-For hop.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, per_minute: int, webhook_per_minute: int):
        super().__init__(app)
        self._per_minute = per_minute
        self._webhook_per_minute = webhook_per_minute
        # key -> (window_start_minute, count)
        self._buckets: dict[str, tuple[int, int]] = {}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Never rate-limit health/readiness probes.
        if path.startswith("/health"):
            return await call_next(request)

        limit = (
            self._webhook_per_minute
            if path.startswith("/webhooks")
            else self._per_minute
        )
        window = int(time.time() // 60)
        key = f"{_client_ip(request)}:{'wh' if path.startswith('/webhooks') else 'api'}"

        start, count = self._buckets.get(key, (window, 0))
        if start != window:
            start, count = window, 0
        count += 1
        self._buckets[key] = (start, count)

        # Opportunistic cleanup so the dict can't grow unbounded.
        if len(self._buckets) > 10_000:
            self._buckets = {
                k: v for k, v in self._buckets.items() if v[0] == window
            }

        if count > limit:
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": "Too many requests — slow down.",
                        "details": {},
                    }
                },
            )
        return await call_next(request)
