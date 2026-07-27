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

from app.auth.permissions import VIEW_CALLS, require_permission
from app.config import get_settings
from app.dependencies import get_current_practice
from app.models.practice import Practice
from app.models.user import User

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
    # RBAC: starting a browser test-call needs at least call-view access
    # (everyone but unprovisioned). Kept low so any clinic role can demo the
    # receptionist; the action is harmless beyond consuming Retell minutes.
    _: User = Depends(require_permission(VIEW_CALLS)),
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
        # Same per-clinic variables the phone inbound webhook serves — without
        # them the browser demo would SPEAK literal "{{agent_name}}"/KB refs.
        from app.services.llm.dynamic_vars import build_dynamic_variables

        dyn = build_dynamic_variables(_practice)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{RETELL_BASE}/v2/create-web-call",
                headers={"Authorization": f"Bearer {settings.retell_api_key}"},
                json={
                    "agent_id": settings.retell_agent_id,
                    "retell_llm_dynamic_variables": dyn,
                    # Web calls all use the shared demo agent, so the call_ended
                    # webhook can't resolve the clinic from agent_id (it refuses to
                    # guess with 2+ practices). Carry the practice explicitly so the
                    # call logs under THIS clinic instead of an orphan row.
                    "metadata": {"practice_id": str(_practice.id)},
                },
            )
        if resp.status_code >= 400:
            logger.warning("create-web-call failed: %s %s", resp.status_code, resp.text[:300])
            from app.observability.alerts import record_alert
            record_alert("web_call_failed", f"retell_status={resp.status_code}")
            # Surface Retell's status so a misconfig (401 wrong key / 404 wrong
            # agent id — e.g. env still points at the OLD Retell account) is visible
            # instead of a vague "try again". The body is never echoed (may leak).
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=f"Voice demo unavailable (provider returned {resp.status_code}).",
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
