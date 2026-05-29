from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_practice, get_tenant_db
from app.models.booking import Booking
from app.models.call import Call
from app.models.patient import Patient
from app.models.practice import Practice
from app.schemas.call import CallDetail, CallListResponse, CallSummary, TranscriptTurn
from app.utils.redact import redact_name

router = APIRouter(prefix="/api/calls", tags=["calls"])


def _parse_transcript(transcript_jsonb: list | dict | None) -> list[TranscriptTurn]:
    """Convert transcript_jsonb to typed transcript turns.

    Retell stores transcripts as a list of dicts with keys:
      role (str), content (str), words (list, optional)
    The API_CONTRACT uses "text" for the content field.
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


@router.get("", response_model=CallListResponse)
async def list_calls(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    direction: str | None = Query(default=None),
    status: str | None = Query(default=None),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> CallListResponse:
    base = select(Call).where(Call.practice_id == practice.id)
    if direction:
        base = base.where(Call.direction == direction)
    if status:
        base = base.where(Call.status == status)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    rows = (
        await db.execute(
            base.order_by(Call.started_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    summaries: list[CallSummary] = []
    for call in rows:
        patient_name = None
        if call.patient_id:
            patient = (
                await db.execute(select(Patient).where(Patient.id == call.patient_id))
            ).scalar_one_or_none()
            if patient:
                patient_name = redact_name(patient.first_name, patient.last_name)
        booking = (
            await db.execute(
                select(Booking.id).where(Booking.source_call_id == call.id)
            )
        ).scalar_one_or_none()
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
            )
        )

    return CallListResponse(
        calls=summaries, total=total, has_more=(offset + len(rows)) < total
    )


@router.get("/{call_id}", response_model=CallDetail)
async def get_call(
    call_id: str,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> CallDetail:
    """Return full call details including parsed transcript. 404 if not found or wrong tenant."""
    result = await db.execute(
        select(Call).where(Call.id == call_id, Call.practice_id == practice.id)
    )
    call = result.scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found.")

    patient_name = None
    if call.patient_id:
        patient = (
            await db.execute(select(Patient).where(Patient.id == call.patient_id))
        ).scalar_one_or_none()
        if patient:
            patient_name = redact_name(patient.first_name, patient.last_name)

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
        transcript=_parse_transcript(call.transcript_jsonb),
    )
