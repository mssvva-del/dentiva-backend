"""The ACTIVE rate limiter — slowapi, mounted unconditionally in app.main.

There used to be two limiters: this one, and a custom middleware gated behind
RATE_LIMIT_ENABLED that defaulted to off. The old version of this file tested
the gated one — so when the suite disabled the live limiter (it was failing
tests by their position in the queue, not their behaviour), the thing actually
guarding production was covered by nothing, while a test named "ratelimit"
passed and said otherwise. The custom middleware is deleted; this now tests the
limiter that answers real traffic.

The suite-wide disable stays: conftest sets limiter.enabled = False so 950
tests do not spend one shared 120/minute budget from 127.0.0.1. This file turns
it back on for exactly its own duration.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.middleware.rate_limit import limiter, rate_limit_exceeded_handler


def _build_app() -> FastAPI:
    """Mini app wired the same way app.main wires the real one."""
    app = FastAPI()
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    @app.get("/api/thing")
    @limiter.limit("3/minute")
    async def thing(request: Request, response: Response):
        # Both annotations matter. Without `request: Request` FastAPI reads it
        # as a query parameter and answers 422 before the limiter runs; without
        # `response` slowapi cannot stamp its rate-limit headers and raises.
        return {"ok": True}

    return app


@pytest.fixture
def _limiter_on():
    """Enable for this test only, and ALWAYS restore.

    Leaving it on would re-poison the rest of the suite with the exact
    order-dependent failures the disable exists to prevent.
    """
    limiter.enabled = True
    # A fresh window: previous tests in this process may have spent the budget.
    limiter.reset()
    try:
        yield
    finally:
        limiter.enabled = False


async def test_the_limit_actually_refuses(_limiter_on):
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(3):
            assert (await client.get("/api/thing")).status_code == 200
        refused = await client.get("/api/thing")
        assert refused.status_code == 429


async def test_the_refusal_speaks_the_error_envelope(_limiter_on):
    """A 429 goes to the same dashboards as every other error. A bare string
    body would be the one response in the API that zod cannot parse."""
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(3):
            await client.get("/api/thing")
        refused = await client.get("/api/thing")
        assert refused.status_code == 429
        body = refused.json()
        assert body["error"]["code"] == "RATE_LIMITED"


async def test_disabled_means_disabled():
    """What the whole suite relies on: with the flag off, no budget is spent."""
    assert limiter.enabled is False
    app = _build_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(10):
            assert (await client.get("/api/thing")).status_code == 200


async def test_a_fleets_call_traffic_is_not_throttled_by_the_human_default(
    _limiter_on, client, db_session
):
    """Every Retell request — each tool call inside each conversation, each
    lifecycle event, the pre-answer inbound webhook — arrives from Retell's
    egress IPs. Under the global default (120/minute per IP) a 200-clinic
    morning is a 429 storm: the agent fails a tool while a patient is mid-
    sentence, and dropped lifecycle events are calls that never got recorded.

    The security control on these routes is the SIGNATURE. The rate limit is a
    runaway backstop and has to sit far above a whole fleet's legitimate rate.

    130 requests from one IP: past the 120/min default, well under the webhook
    ceiling. Every one must be answered, none with 429."""
    for i in range(130):
        r = await client.post(
            "/webhooks/retell/inbound",
            json={"call_inbound": {"agent_id": None, "to_number": f"+1617555{i:04d}"}},
        )
        assert r.status_code != 429, f"throttled at request {i + 1}"
        assert r.status_code == 200
