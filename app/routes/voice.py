"""Voice demo — mint a Retell web-call token so the dashboard can start a live
browser call with the AI receptionist (great for screen-share demos).

The Retell API key stays server-side; the browser only ever receives a
short-lived access token for one call.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.permissions import VIEW_CALLS, require_permission
from app.config import get_settings
from app.db import async_session_factory
from app.dependencies import get_current_practice
from app.middleware.rate_limit import limit_public
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
    """Public diagnostic: can the browser demo start a call?

    One coarse boolean on purpose. It used to also report which half of the
    configuration was missing and whether the demo was open without auth — a free
    map of the deployment for anyone who asks, in exchange for telling us nothing
    the single flag doesn't. Detail lives in the authenticated admin view.
    """
    settings = get_settings()
    return {"configured": bool(settings.retell_api_key and settings.retell_agent_id)}


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
    return _as_response(await _mint_web_call(_practice))


def _as_response(data: dict) -> WebCallResponse:
    token = data.get("access_token")
    if not token:
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Voice service returned no token.",
        )
    return WebCallResponse(
        access_token=token, call_id=data.get("call_id"), agent_id=data.get("agent_id"),
    )


# The clerk org the seed gives the fictional demo clinic. The website's "talk to
# the receptionist" button calls THIS practice and no other: it is the one
# clinic with no patients, no PMS and nothing a stranger could learn.
DEMO_CLINIC_ORG = "demo_org_dentiva"


async def _mint_web_call(practice: Practice) -> dict:
    """One Retell web call for this clinic; the browser gets a token for it."""
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

        dyn = build_dynamic_variables(practice)
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
                    "metadata": {"practice_id": str(practice.id)},
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
        return resp.json()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("create-web-call error")
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Voice service unreachable.",
        ) from exc


@router.post("/public-web-call", response_model=WebCallResponse)
# Open to the internet and every call costs Retell minutes; a visitor who
# starts more than two calls a minute is not evaluating the product.
@limit_public("2/minute")
async def create_public_web_call(
    request: Request,
    response: Response,  # slowapi writes its headers here
) -> WebCallResponse:
    """A browser call with the receptionist for a visitor on the marketing site.

    Unauthenticated by design, and pinned to the demo clinic: the visitor is
    nobody's staff, so they get the one practice that is nobody's either.
    """
    async with async_session_factory() as session:
        practice = (await session.execute(
            select(Practice).where(Practice.clerk_org_id == DEMO_CLINIC_ORG)
        )).scalar_one_or_none()
    if practice is None:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The demo isn't available right now.",
        )
    return _as_response(await _mint_web_call(practice))
