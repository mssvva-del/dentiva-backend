from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_practice, get_tenant_db
from app.models.booking import Booking
from app.models.patient import Patient
from app.models.practice import Practice
from app.schemas.booking import BookingListResponse, BookingSummary
from app.utils.redact import redact_name

router = APIRouter(prefix="/api/bookings", tags=["bookings"])


@router.get("", response_model=BookingListResponse)
async def list_bookings(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    status: str | None = Query(default=None),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> BookingListResponse:
    today = datetime.now(UTC).date()
    start = from_date or today
    end = to_date or (today + timedelta(days=30))
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(end, datetime.max.time(), tzinfo=UTC)

    base = (
        select(Booking)
        .where(Booking.practice_id == practice.id)
        .where(Booking.appointment_at >= start_dt)
        .where(Booking.appointment_at <= end_dt)
    )
    if status:
        base = base.where(Booking.status == status)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    rows = (
        await db.execute(base.order_by(Booking.appointment_at.asc()))
    ).scalars().all()

    summaries: list[BookingSummary] = []
    for b in rows:
        patient = (
            await db.execute(select(Patient).where(Patient.id == b.patient_id))
        ).scalar_one_or_none()
        name = redact_name(patient.first_name, patient.last_name) if patient else None
        summaries.append(
            BookingSummary(
                id=str(b.id),
                patient_name_redacted=name,
                patient_id=str(b.patient_id),
                appointment_at=b.appointment_at,
                duration_minutes=b.duration_minutes,
                procedure_type=b.procedure_type,
                provider_name=b.provider_name,
                status=b.status,
                source=b.source,
                source_call_id=str(b.source_call_id) if b.source_call_id else None,
                created_at=b.created_at,
            )
        )

    return BookingListResponse(bookings=summaries, total=total)


@router.get("/{booking_id}", response_model=BookingSummary)
async def get_booking(
    booking_id: str,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> BookingSummary:
    """Return full booking details. 404 if not found or wrong tenant."""
    result = await db.execute(
        select(Booking).where(
            Booking.id == booking_id,
            Booking.practice_id == practice.id,
        )
    )
    b = result.scalar_one_or_none()
    if b is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Booking not found.",
        )

    patient = (
        await db.execute(select(Patient).where(Patient.id == b.patient_id))
    ).scalar_one_or_none()
    name = redact_name(patient.first_name, patient.last_name) if patient else None

    return BookingSummary(
        id=str(b.id),
        patient_name_redacted=name,
        patient_id=str(b.patient_id),
        appointment_at=b.appointment_at,
        duration_minutes=b.duration_minutes,
        procedure_type=b.procedure_type,
        provider_name=b.provider_name,
        status=b.status,
        source=b.source,
        source_call_id=str(b.source_call_id) if b.source_call_id else None,
        created_at=b.created_at,
    )
