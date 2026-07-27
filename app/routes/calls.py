from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import false as sa_false
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import VIEW_CALLS, require_permission
from app.dependencies import get_current_practice, get_tenant_db
from app.models.booking import Booking
from app.models.call import Call
from app.models.patient import Patient
from app.models.practice import Practice
from app.models.user import User
from app.schemas.call import (
    ActiveCallsResponse,
    ActiveCallSummary,
    CallDetail,
    CallListResponse,
    CallSummary,
    TranscriptTurn,
)
from app.utils.crypto import phone_hmac
from app.utils.redact import redact_name, redact_pii_text

router = APIRouter(prefix="/api/calls", tags=["calls"])


def _parse_transcript(
    transcript_jsonb: list | dict | None,
    extra_terms: list[str] | None = None,
) -> list[TranscriptTurn]:
    """Convert transcript_jsonb to typed transcript turns.

    Retell stores transcripts as a list of dicts with keys:
      role (str), content (str), words (list, optional)
    The API_CONTRACT uses "text" for the content field.

    PHI: the spoken text is free-form and may contain phone numbers, emails, or
    the patient's name, so each turn's text is run through redact_pii_text before
    it leaves the API. ``extra_terms`` (the patient's known name) masks names that
    can't be detected by shape alone.
    """
    if not transcript_jsonb:
        return []
    if isinstance(transcript_jsonb, dict):
        # Safety: if stored as a single dict, wrap it.
        transcript_jsonb = [transcript_jsonb]
    turns: list[TranscriptTurn] = []
    for item in transcript_jsonb:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "unknown")
        text = item.get("content") or item.get("text") or ""
        text = redact_pii_text(text, extra_terms=extra_terms) or ""
        # "agent" from Retell -> rename for API consumers.
        if role == "agent":
            role = "agent"
        elif role == "user":
            role = "patient"
        words = item.get("words") or []
        ts: float | None = None
        if words and isinstance(words, list) and isinstance(words[0], dict):
            ts = words[0].get("start")
        turns.append(TranscriptTurn(role=role, text=text, ts=ts))
    return turns


async def _query_calls(
    practice: Practice, db: AsyncSession, *,
    limit: int, offset: int, direction: str | None, status: str | None, search: str | None,
) -> CallListResponse:
    base = select(Call).where(Call.practice_id == practice.id)
    if direction:
        base = base.where(Call.direction == direction)
    if status:
        base = base.where(Call.status == status)
    if search:
        # from_number/to_number are encrypted now, so a substring ILIKE is no longer
        # possible. Search a phone query by its deterministic hash (exact match on the
        # normalized caller number). A non-phone query matches nothing.
        h = phone_hmac(search)
        base = base.where(Call.caller_number_hmac == h) if h else base.where(sa_false())

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    rows = (
        await db.execute(
            base.order_by(Call.started_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    # Batch-load names + bookings for the whole page (was an N+1: 2 queries per row).
    patient_ids = {c.patient_id for c in rows if c.patient_id}
    call_ids = [c.id for c in rows]
    names: dict = {}
    if patient_ids:
        for p in (await db.execute(
            select(Patient).where(Patient.id.in_(patient_ids))
        )).scalars():
            names[p.id] = redact_name(p.first_name, p.last_name)
    booking_ids: dict = {}
    if call_ids:
        for bid, scid in (await db.execute(
            select(Booking.id, Booking.source_call_id).where(Booking.source_call_id.in_(call_ids))
        )).all():
            booking_ids.setdefault(scid, bid)  # one booking per call is enough for the badge

    summaries: list[CallSummary] = []
    for call in rows:
        patient_name = names.get(call.patient_id) if call.patient_id else None
        booking = booking_ids.get(call.id)
        summaries.append(
            CallSummary(
                id=str(call.id),
                direction=call.direction,
                from_number=call.from_number,
                to_number=call.to_number,
                started_at=call.started_at,
                duration_seconds=call.duration_seconds,
                status=call.status,
                patient_name_redacted=patient_name,
                patient_id=str(call.patient_id) if call.patient_id else None,
                outcome=call.outcome,
                booking_id=str(booking) if booking else None,
                transcript_available=call.transcript_jsonb is not None,
                call_intent=call.call_intent,
                patient_sentiment=call.patient_sentiment,
                escalation_needed=call.escalation_needed,
            )
        )

    return CallListResponse(
        calls=summaries, total=total, has_more=(offset + len(rows)) < total
    )


# WHY both a permission gate AND a practice_id filter (in _query_calls): defense in
# depth. require_permission stops a wrong-role user; the practice_id WHERE (RLS via
# get_tenant_db) stops cross-tenant reads. Neither alone is enough.
@router.get("", response_model=CallListResponse)
async def list_calls(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    direction: str | None = Query(default=None),
    status: str | None = Query(default=None),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    _user: User = Depends(require_permission(VIEW_CALLS)),
) -> CallListResponse:
    """List calls. Phone search is intentionally NOT a query param here — a raw
    number in the URL would land in access logs / browser history (PHI). Search by
    phone via POST /search instead."""
    return await _query_calls(practice, db, limit=limit, offset=offset,
                              direction=direction, status=status, search=None)


class CallSearch(BaseModel):
    search: str | None = None
    direction: str | None = None
    status: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


@router.post("/search", response_model=CallListResponse)
async def search_calls(
    body: CallSearch,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    _user: User = Depends(require_permission(VIEW_CALLS)),
) -> CallListResponse:
    """Filtered call list. The phone search travels in the request BODY (never the
    URL) so PHI stays out of access logs and browser history."""
    return await _query_calls(practice, db, limit=body.limit, offset=body.offset,
                              direction=body.direction, status=body.status,
                              search=body.search)


@router.get("/active", response_model=ActiveCallsResponse)
async def list_active_calls(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    _user: User = Depends(require_permission(VIEW_CALLS)),
) -> ActiveCallsResponse:
    """Return calls currently in progress (started but not yet ended). Max 10."""
    rows = (
        await db.execute(
            select(Call)
            .where(Call.practice_id == practice.id, Call.status == "in_progress")
            .order_by(Call.started_at.asc())
            .limit(10)
        )
    ).scalars().all()

    now = datetime.now(UTC)
    summaries: list[ActiveCallSummary] = []
    for call in rows:
        started = call.started_at
        # Ensure started_at is timezone-aware for arithmetic.
        if started.tzinfo is None:

            started = started.replace(tzinfo=UTC)
        duration = max(0, int((now - started).total_seconds()))
        summaries.append(
            ActiveCallSummary(
                id=str(call.id),
                retell_call_id=call.retell_call_id,
                direction=call.direction,
                from_number=call.from_number,
                started_at=call.started_at,
                duration_seconds_so_far=duration,
            )
        )

    return ActiveCallsResponse(active_calls=summaries, count=len(summaries))


@router.get("/{call_id}", response_model=CallDetail)
async def get_call(
    call_id: str,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    _user: User = Depends(require_permission(VIEW_CALLS)),
) -> CallDetail:
    """Return full call details including parsed transcript. 404 if not found or wrong tenant."""
    result = await db.execute(
        select(Call).where(Call.id == call_id, Call.practice_id == practice.id)
    )
    call = result.scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found.")

    patient_name = None
    name_terms: list[str] = []
    if call.patient_id:
        patient = (
            await db.execute(select(Patient).where(Patient.id == call.patient_id))
        ).scalar_one_or_none()
        if patient:
            patient_name = redact_name(patient.first_name, patient.last_name)
            # Known name → mask any spoken occurrences in the transcript too.
            name_terms = [t for t in (patient.first_name, patient.last_name) if t]

    booking = (
        await db.execute(
            select(Booking.id).where(Booking.source_call_id == call.id)
        )
    ).scalar_one_or_none()

    recording_url: str | None = None
    if call.recording_path:
        # In production this would be a signed S3 URL; for weekend mode return the path as-is.
        recording_url = call.recording_path

    return CallDetail(
        id=str(call.id),
        direction=call.direction,
        from_number=call.from_number,
        to_number=call.to_number,
        started_at=call.started_at,
        ended_at=call.ended_at,
        duration_seconds=call.duration_seconds,
        status=call.status,
        patient_name_redacted=patient_name,
        patient_id=str(call.patient_id) if call.patient_id else None,
        outcome=call.outcome,
        booking_id=str(booking) if booking else None,
        recording_url=recording_url,
        transcript=_parse_transcript(call.transcript_jsonb, extra_terms=name_terms),
        call_intent=call.call_intent,
        patient_sentiment=call.patient_sentiment,
        escalation_needed=call.escalation_needed,
    )
