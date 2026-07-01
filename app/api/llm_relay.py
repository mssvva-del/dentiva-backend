"""Retell custom-LLM WebSocket relay → Groq.

Lets the receptionist run on OUR Groq loop (Llama-3.3-70b, sub-second, tunable)
instead of Retell's managed model. Retell opens a WebSocket per call and speaks its
custom-LLM protocol; we translate each turn into an OpenAI-shaped chat request,
stream Groq's tokens back as Retell ``response`` frames (low time-to-first-word).

Protocol (Retell custom-LLM v2), messages are JSON:
  in : interaction_type ∈ {call_details, ping_pong, update_only,
       response_required, reminder_required}
  out: response_type ∈ {config, ping_pong, response}

Gated by ENABLE_LLM_RELAY — mounted only when true, so the demo keeps using the
Retell-managed model until we deliberately switch a clinic over.

SCOPE (this iteration): conversational streaming. TOOL-CALLING (book_appointment
etc.) is NOT wired here yet — those still run over the existing /webhooks/retell
function-call path. Wiring tools into the relay reuses those same handlers and is
the live-loop follow-up (it can only be validated against a real Retell call).

AUTH (pre-prod follow-up): the WebSocket has no auth today. Safe while gated off,
and when enabled the blast radius is Groq token spend + made-up conversation (no
PHI write — tools stay on the signed webhook). BEFORE enabling in prod, add a
shared-secret check on the connection (Retell supports a custom header / query
param on the custom-LLM URL).
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.models.practice import Practice
from app.services.llm.groq_client import GroqError, stream_chat
from app.services.llm.prompt import build_system_prompt, transcript_to_messages
from app.webhooks.retell import _resolve_practice

logger = logging.getLogger("dentiva.llm.relay")

router = APIRouter()

_DEFAULT_PROMPT = (
    "You are the AI receptionist for a US dental practice. Be warm and concise; "
    "one question at a time. English or Spanish only, following the caller."
)
# Spoken if Groq fails/stalls mid-turn before any token went out — never go silent.
_FALLBACK = "I'm sorry, I'm having a little trouble — let me connect you to our team."

# Wall-clock budgets so a stalled Groq never leaves the caller in dead air. A live
# call can't wait httpx's full read timeout — cap the first token and inter-token
# gaps tightly and speak the fallback on breach.
_FIRST_TOKEN_TIMEOUT = 5.0
_GAP_TIMEOUT = 5.0


async def _safe_send(ws: WebSocket, frame: dict) -> bool:
    """Send a frame; return False if the socket is already gone (caller aborts the
    turn instead of raising out of the receive loop)."""
    try:
        await ws.send_json(frame)
        return True
    except Exception:  # noqa: BLE001 — Retell hung up mid-turn; stop cleanly
        return False


def _resp_frame(response_id, content: str, complete: bool) -> dict:
    return {
        "response_type": "response",
        "response_id": response_id,
        "content": content,
        "content_complete": complete,
    }


async def _respond(ws: WebSocket, msg: dict, system_prompt: str) -> None:
    """Generate one assistant turn: stream Groq tokens as Retell response frames,
    always closing with a content_complete frame. Enforces first-token + inter-token
    timeouts and tolerates a mid-turn socket close."""
    response_id = msg.get("response_id")
    messages = transcript_to_messages(system_prompt, msg.get("transcript", []))
    sent_any = False
    agen = stream_chat(messages).__aiter__()
    try:
        while True:
            budget = _FIRST_TOKEN_TIMEOUT if not sent_any else _GAP_TIMEOUT
            try:
                delta = await asyncio.wait_for(agen.__anext__(), timeout=budget)
            except StopAsyncIteration:
                break
            except (TimeoutError, GroqError) as exc:
                logger.warning("relay: turn aborted (%s)", type(exc).__name__)
                break
            if not await _safe_send(ws, _resp_frame(response_id, delta, False)):
                return  # socket gone — nothing more to do
            sent_any = True
    finally:
        await agen.aclose()  # ensure the httpx stream is torn down
    if not sent_any:
        await _safe_send(ws, _resp_frame(response_id, _FALLBACK, False))
    # Always close the turn so Retell stops waiting.
    await _safe_send(ws, _resp_frame(response_id, "", True))


async def _resolve_prompt(call_details: dict) -> str:
    """Resolve this call's clinic-aware system prompt from the call_details frame."""
    call = call_details.get("call") or {}
    agent_id = call.get("agent_id")
    practice: Practice | None = await _resolve_practice(agent_id)
    return build_system_prompt(practice) if practice else _DEFAULT_PROMPT


@router.websocket("/ws/retell-llm/{call_id}")
async def retell_llm(websocket: WebSocket, call_id: str) -> None:
    await websocket.accept()
    # Ask Retell to send call_details and to auto-reconnect on transient drops.
    await websocket.send_json(
        {"response_type": "config", "config": {"auto_reconnect": True, "call_details": True}}
    )
    system_prompt = _DEFAULT_PROMPT
    try:
        while True:
            try:
                msg = await websocket.receive_json()
            except json.JSONDecodeError:
                # A single malformed frame must not tear down an in-progress call.
                logger.warning("relay: dropped a malformed frame on call %s", call_id)
                continue
            itype = msg.get("interaction_type")
            if itype == "ping_pong":
                await _safe_send(
                    websocket,
                    {"response_type": "ping_pong", "timestamp": msg.get("timestamp")},
                )
            elif itype == "call_details":
                system_prompt = await _resolve_prompt(msg)
            elif itype in ("response_required", "reminder_required"):
                await _respond(websocket, msg, system_prompt)
            elif itype == "update_only":
                pass  # transcript sync only — no response needed
            else:
                logger.debug("relay: unhandled interaction_type=%r", itype)
    except WebSocketDisconnect:
        logger.info("relay: call %s disconnected", call_id)
