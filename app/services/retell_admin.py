"""Retell agent administration from OUR admin panel (voice model switching).

Thin httpx client against Retell's API (same injectable-transport pattern as
stripe_client). The admin panel changes the LLM model behind the live agent and
republishes it — no Retell dashboard access needed.

Model allowlist mirrors Retell's supported enum (docs, 2026-07). Keep in sync
when Retell adds models; an unknown value 422s before any API call.
"""

from __future__ import annotations

import httpx

from app.config import get_settings

_RETELL_API = "https://api.retellai.com"

# BUDGET allowlist (Sergio 2026-07-07: model cost must stay ≤ ~$0.05/min).
# Curated from Retell's supported enum + pricing page; order = admin-UI display,
# best budget quality first. Deliberately EXCLUDED: sonnet tier / gpt-5.5/5.4/5.2
# (blow the per-minute budget) and the nano tier (the digit-mangling class we
# already burned on). Retell $/min for the model component in comments.
ALLOWED_VOICE_MODELS: tuple[str, ...] = (
    "gpt-5.1",            # $0.040 — flagship-class at half sonnet price (default)
    "gpt-4.1",            # $0.045 — proven, very instruction-obedient
    "claude-4.5-haiku",   # $0.025 — fastest latency, solid compliance
    "gemini-3.0-flash",   # $0.027 — cheap+fast, weaker on long rule sets
    "gpt-5-mini",         # $0.012 — floor option, a tier above old gpt-4o-mini
)


class RetellError(Exception):
    """Retell request failed; status_code lets routes map 5xx→502, 4xx→422."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetellNotConfigured(Exception):
    """RETELL_API_KEY missing — surfaced as a clean 503."""


async def _request(
    method: str, path: str, payload: dict | None = None,
    *, transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    settings = get_settings()
    if not settings.retell_api_key:
        raise RetellNotConfigured("RETELL_API_KEY not set")
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as client:
            resp = await client.request(
                method, f"{_RETELL_API}{path}", json=payload,
                headers={"Authorization": f"Bearer {settings.retell_api_key}"},
            )
    except httpx.HTTPError as exc:
        raise RetellError(f"Retell unreachable: {type(exc).__name__}",
                          status_code=502) from exc
    if resp.status_code >= 400:
        raise RetellError(f"Retell {resp.status_code}: {resp.text[:200]}",
                          status_code=resp.status_code)
    return resp.json() if resp.content else {}


async def get_agent(agent_id: str, *, transport=None) -> dict:
    return await _request("GET", f"/get-agent/{agent_id}", transport=transport)


async def get_llm(llm_id: str, *, transport=None) -> dict:
    return await _request("GET", f"/get-retell-llm/{llm_id}", transport=transport)


async def set_llm_model(llm_id: str, model: str, *, transport=None) -> dict:
    return await _request("PATCH", f"/update-retell-llm/{llm_id}",
                          {"model": model}, transport=transport)


async def publish_agent(agent_id: str, *, transport=None) -> dict:
    return await _request("POST", f"/publish-agent/{agent_id}", {},
                          transport=transport)
