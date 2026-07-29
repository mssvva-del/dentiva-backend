"""In-app help assistant — answers questions about using Dentovox.

Scope is deliberately narrow: it reads a curated product document and nothing
else. It never touches practice data, so a bug here cannot leak PHI, and the
clinic's own patient information never reaches the model.

Anthropic only (the model sees no PHI either way, but we keep one vendor path for
LLM traffic). Rate-limited per practice: an assistant is a nice place to burn
tokens by accident.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.permissions import VIEW_DASHBOARD, require_permission
from app.config import get_settings
from app.dependencies import get_current_practice
from app.middleware.rate_limit import limit_authenticated
from app.models.practice import Practice
from app.models.user import User
from app.services.assistant.knowledge import SYSTEM_PROMPT

logger = logging.getLogger("dentiva.assistant")

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

# Enough for a real back-and-forth, short enough that a runaway client can't
# resend a novel. The window is client-held: the assistant is stateless.
_MAX_TURNS = 12
_MAX_CHARS = 2000


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=_MAX_CHARS)


class AskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=_MAX_CHARS)
    history: list[Turn] = Field(default_factory=list)


class AskResponse(BaseModel):
    reply: str


@router.post("/ask", response_model=AskResponse)
# Tighter than the global default: every question costs tokens.
@limit_authenticated("20/minute")
async def ask(
    request: Request,
    response: Response,  # slowapi writes its headers here
    body: AskRequest,
    practice: Practice = Depends(get_current_practice),
    _user: User = Depends(require_permission(VIEW_DASHBOARD)),
) -> AskResponse:
    """Answer a product question. Any signed-in clinic user may ask."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The assistant isn't available right now.",
        )

    # Only the clinic NAME is passed through, so answers can say "your practice"
    # naturally. No patient data, no counts, nothing else about this tenant.
    system = f"{SYSTEM_PROMPT}\n\nThe clinic you are helping is: {practice.name}."

    messages = [
        {"role": t.role, "content": t.content} for t in body.history[-_MAX_TURNS:]
    ]
    messages.append({"role": "user", "content": body.message})

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-5",
                    "max_tokens": 600,
                    "system": system,
                    "messages": messages,
                },
            )
        if resp.status_code >= 400:
            logger.warning("assistant: anthropic %s", resp.status_code)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The assistant is having trouble — please try again.",
            )
        blocks = resp.json().get("content") or []
        reply = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("assistant: request failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The assistant is having trouble — please try again.",
        ) from exc

    if not reply.strip():
        reply = (
            "I didn't catch that — could you rephrase? For anything I can't "
            "answer, email support@dentovox.com."
        )
    return AskResponse(reply=reply.strip())
