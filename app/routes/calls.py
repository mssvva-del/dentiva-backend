from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_practice, get_tenant_db
from app.models.booking import Booking
from app.models.call import Call
from app.models.patient import Patient
from app.models.practice import Practice
from app.schemas.call import CallListResponse, CallSummary
from app.utils.redact import redact_name

router = APIRouter(prefix="/api/calls", tags=["calls"])


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
