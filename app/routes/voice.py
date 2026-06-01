"""Voice demo — mint a Retell web-call token so the dashboard can start a live
browser call with the AI receptionist (great for screen-share demos).

The Retell API key stays server-side; the browser only ever receives a
short-lived access token for one call.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from pydantic import BaseModel

from app.config import get_settings
from app.dependencies import get_current_practice
from app.models.practice import Practice

logger = logging.getLogger("dentiva.routes.voice")

router = APIRouter(prefix="/api/voice", tags=["voice"])

RETELL_BASE = "https://api.retellai.com"


class WebCallResponse(BaseModel):
    access_token: str
    call_id: str | None = None
    agent_id: str | None = None


@router.get("/status")
async def voice_status() -> dict:
    """Public diagnostic: is the voice demo configured? Exposes only booleans
    (no secrets) so config can be verified remotely without auth."""
    settings = get_settings()
    return {
        "configured": bool(settings.retell_api_key and settings.retell_agent_id),
        "has_key": bool(settings.retell_api_key),
        "has_agent": bool(settings.retell_agent_id),
        "demo_open_access": settings.demo_open_access,
    }


@router.post("/web-call", response_model=WebCallResponse)
async def create_web_call(
    _practice: Practice = Depends(get_current_practice),
) -> WebCallResponse:
    """Create a Retell web call and return its browser access token.

    Used by the dashboard "Talk to the AI receptionist" button to start a live
    voice demo in the browser (mic). Auth-gated so only signed-in staff can
    spin up a call.
    """
    settings = get_settings()
    if not (settings.retell_api_key and settings.retell_agent_id):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice demo not configured (missing Retell key/agent).",
        )

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{RETELL_BASE}/v2/create-web-call",
                headers={"Authorization": f"Bearer {settings.retell_api_key}"},
                json={"agent_id": settings.retell_agent_id},
            )
        if resp.status_code >= 400:
            logger.warning("create-web-call failed: %s %s", resp.status_code, resp.text[:200])
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail="Could not start the voice demo. Try again.",
            )
        data = resp.json()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("create-web-call error")
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Voice service unreachable.",
        ) from exc

    token = data.get("access_token")
    if not token:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Voice service returned no token.",
        )
    return WebCallResponse(
        access_token=token,
        call_id=data.get("call_id"),
        agent_id=data.get("agent_id"),
    )
