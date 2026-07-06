"""Public lead-capture endpoint — the marketing-site demo form posts here.

Unauthenticated (the site is public), so it is rate-limited per IP and has a
honeypot to blunt bots. Leads are OUR business data (no tenant / no PHI); admins
read + work them via /api/admin/leads.
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

import app.db as _app_db
from app.middleware.rate_limit import limit_public
from app.models.lead import Lead

router = APIRouter(prefix="/api/leads", tags=["leads"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LeadCreate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    clinic_name: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=2000)
    source: str | None = Field(default=None, max_length=40)
    # Honeypot: a hidden field real users never fill. If set → a bot; we accept the
    # request (don't tip off the bot) but silently drop it.
    website: str | None = Field(default=None, max_length=200)


class LeadAck(BaseModel):
    ok: bool


@router.post("", response_model=LeadAck)
@limit_public("10/minute")
async def create_lead(
    request: Request, response: Response, payload: LeadCreate
) -> LeadAck:
    """Accept a lead from the public site form. Always returns ok (even for spam /
    empty / honeypot) so the endpoint reveals nothing to probers."""
    # Honeypot filled → drop silently.
    if payload.website:
        return LeadAck(ok=True)
    # Need at least one usable contact; a valid email or any phone.
    email = payload.email if payload.email and _EMAIL_RE.match(payload.email) else None
    phone = payload.phone or None
    if not (email or phone):
        return LeadAck(ok=True)  # nothing to act on — accept quietly, don't store junk

    lead = Lead(
        id=uuid.uuid4(),
        name=payload.name or None,
        email=email,
        phone=phone,
        clinic_name=payload.clinic_name or None,
        message=payload.message or None,
        source=(payload.source or "site")[:40],
        status="new",
    )
    async with _app_db.async_session_factory() as session:
        session.add(lead)
        await session.commit()
    return LeadAck(ok=True)
