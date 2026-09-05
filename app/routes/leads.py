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
from app.observability.alerts import record_alert

router = APIRouter(prefix="/api/leads", tags=["leads"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LeadCreate(BaseModel):
    """What the site's demo form sends, under the names it already uses.

    The form posted to an email inbox for months with fields called
    ``practice``, ``locations``, ``referral`` and ``description``, plus the
    first-touch attribution the site's script attaches. Accepting those names
    directly means the site changes one URL, not its form.
    """

    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=40)
    clinic_name: str | None = Field(default=None, max_length=200)
    practice: str | None = Field(default=None, max_length=200)      # site's name for clinic_name
    message: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=2000)  # site's name for message
    locations: str | None = Field(default=None, max_length=40)
    referral: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=40)
    source_landing: str | None = Field(default=None, max_length=300)
    source_referrer: str | None = Field(default=None, max_length=300)
    source_utm: str | None = Field(default=None, max_length=300)
    submitted_from: str | None = Field(default=None, max_length=300)
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

    # The form asks three things the table has no column for. They belong in
    # the message, in front of it — "3 locations, referred by Dr. Patel" is the
    # first thing sales wants to know, not the last.
    extras = []
    if payload.locations:
        extras.append(f"Locations: {payload.locations}")
    if payload.referral:
        extras.append(f"Referral: {payload.referral}")
    body = payload.message or payload.description or ""
    message = "\n".join([*extras, body]).strip() or None

    lead = Lead(
        id=uuid.uuid4(),
        name=payload.name or None,
        email=email,
        phone=phone,
        clinic_name=payload.clinic_name or payload.practice or None,
        message=message,
        source=(payload.source or "site")[:40],
        status="new",
        landing_page=payload.source_landing or None,
        referrer=payload.source_referrer or None,
        utm=payload.source_utm or None,
        submitted_from=payload.submitted_from or None,
    )
    async with _app_db.async_session_factory() as session:
        session.add(lead)
        await session.commit()
    # Nothing else tells anyone. There is no email channel and texting is off
    # until carrier registration; the alert is what the health page and the
    # admin overview read, so a request for a demo is at least visible somewhere
    # the moment it lands.
    record_alert("new_lead", f"source={lead.source} from={lead.submitted_from or '-'}")
    return LeadAck(ok=True)
