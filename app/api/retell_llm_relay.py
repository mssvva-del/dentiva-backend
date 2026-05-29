"""Retell custom-LLM WebSocket relay.

Retell's "custom LLM" engine opens a WebSocket to this server.  We receive
conversation turns and forward them to Groq's OpenAI-compatible
/chat/completions endpoint, then stream the response back in Retell's expected
format.

Retell custom-LLM protocol (https://docs.retellai.com/build-llm-websocket):
  Retell → server:
    { "interaction_type": "call_details",  "call": {...} }
    { "interaction_type": "response_required",
      "response_id": 1,
      "transcript": [{"role": "agent"|"user", "content": "..."}] }
    { "interaction_type": "reminder_required",
      "response_id": 2,
      "transcript": [...] }

  Server → Retell:
    { "response_id": 1, "content": "...", "content_complete": false }
    ...
    { "response_id": 1, "content": "", "content_complete": true }

Endpoint: GET /ws/retell-llm  (WebSocket upgrade)
"""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import get_settings

logger = logging.getLogger("dentiva.api.retell_llm_relay")

router = APIRouter(tags=["retell-llm-relay"])

# ---------------------------------------------------------------------------
# Dental receptionist system prompt (English, Iter 1)
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are Anna, the AI receptionist for Smile Dental, a dental practice.
You answer incoming calls, help patients book or change appointments, and answer
simple questions about the office. You are friendly, efficient, and never pretend
to be a human dentist.

Voice & tone:
- Warm, professional, calm.
- Speak naturally. Use contractions ("I'll", "you're", "let's").
- Keep replies short — one or two sentences, then let the caller talk.
- If you need a moment, say "Let me check that for you."

Capabilities:
- Schedule a new appointment.
- Reschedule or cancel an existing appointment.
- Answer questions about hours, location, and services.
- Take a callback request when you can't help directly.

Hard limits:
- NO medical or dental advice.
- NO pricing quotes.
- NO insurance coverage discussion.
- If a caller describes an emergency (severe pain, swelling, bleeding, trauma):
  "This sounds urgent. Let me get our office to call you right back."
- Never say "As an AI" or explain how you work.
- If you can't help, take a callback request and let them go.

Opening line: "Thank you for calling Smile Dental, this is Anna. How can I help you today?"
"""

_FALLBACK_MESSAGE = (
    "I'm sorry, I'm having a little trouble right now. "
    "Can you give me just a moment?"
)

# Retell interaction types that require an LLM response.
_LLM_TRIGGER_TYPES = {"response_required", "reminder_required"}


def _build_messages(transcript: list[dict]) -> list[dict]:
    """Convert Retell transcript array to OpenAI chat messages format."""
    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for turn in transcript:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        # Retell uses "agent" — map to "assistant".
        if role == "agent":
            role = "assistant"
        messages.append({"role": role, "content": content})
    return messages


async def _stream_groq_response(
    websocket: WebSocket,
    response_id: int,
    transcript: list[dict],
) -> None:
    """Call Groq /chat/completions (streaming) and relay chunks to Retell."""
    settings = get_settings()
    messages = _build_messages(transcript)

    url = f"{settings.groq_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.llm_model_fast,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 200,
        "stream": True,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            async with http.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    logger.error(
                        "Groq returned %s: %s", resp.status_code, error_body[:300]
                    )
                    await _send_fallback(websocket, response_id)
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    delta = (
                        chunk.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                    )
                    if delta:
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "response_id": response_id,
                                    "content": delta,
                                    "content_complete": False,
                                }
                            )
                        )

        # Signal completion.
        await websocket.send_text(
            json.dumps(
                {
                    "response_id": response_id,
                    "content": "",
                    "content_complete": True,
                }
            )
        )

    except httpx.TimeoutException:
        logger.warning("Groq request timed out (response_id=%s).", response_id)
        await _send_fallback(websocket, response_id)
    except Exception:
        logger.exception("Unexpected error in Groq relay (response_id=%s).", response_id)
        await _send_fallback(websocket, response_id)


async def _send_fallback(websocket: WebSocket, response_id: int) -> None:
    """Send a graceful fallback message so Retell doesn't hang."""
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "response_id": response_id,
                    "content": _FALLBACK_MESSAGE,
                    "content_complete": False,
                }
            )
        )
        await websocket.send_text(
            json.dumps(
                {
                    "response_id": response_id,
                    "content": "",
                    "content_complete": True,
                }
            )
        )
    except Exception:
        logger.exception("Failed to send fallback message.")


@router.websocket("/ws/retell-llm")
async def retell_llm_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint consumed by Retell's custom-LLM engine."""
    await websocket.accept()
    logger.info("Retell custom-LLM connection accepted.")

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON message from Retell: %s", raw[:200])
                continue

            interaction_type = msg.get("interaction_type", "")
            logger.debug("Retell → relay: interaction_type=%s", interaction_type)

            if interaction_type == "call_details":
                # Retell sends call metadata at the start. Acknowledge silently.
                logger.info(
                    "call_details received: call_id=%s",
                    msg.get("call", {}).get("call_id", "unknown"),
                )
                continue

            if interaction_type in _LLM_TRIGGER_TYPES:
                response_id = msg.get("response_id", 0)
                transcript = msg.get("transcript", [])
                await _stream_groq_response(websocket, response_id, transcript)

    except WebSocketDisconnect:
        logger.info("Retell custom-LLM WebSocket disconnected.")
    except Exception:
        logger.exception("Unhandled error in retell_llm_websocket; closing connection.")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
