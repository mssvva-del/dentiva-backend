from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import (
    MANAGE_APPOINTMENTS,
    VIEW_APPOINTMENTS,
    require_permission,
)
from app.db import set_tenant
from app.dependencies import get_current_practice, get_tenant_db
from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.enums import BOOKING_STATUSES
from app.models.patient import Patient
from app.models.practice import Practice
from app.models.user import User
from app.schemas.booking import (
    BookingEdit,
    BookingListResponse,
    BookingStatusUpdate,
    BookingSummary,
)
from app.services.booking_edits import (
    apply_cancellation,
    apply_move,
    slot_is_taken,
)
from app.utils.redact import redact_name


def _full_name(patient) -> str | None:
    """The name the front desk needs to greet someone, not an initial."""
    if patient is None:
        return None
    parts = [(patient.first_name or "").strip(), (patient.last_name or "").strip()]
    return " ".join(p for p in parts if p) or None


router = APIRouter(prefix="/api/bookings", tags=["bookings"])

# Statuses staff can set from the dashboard — the same canonical set the DB CHECK
# enforces (no_show powers no-show tracking).
_ALLOWED_STATUSES = BOOKING_STATUSES

# Audit action written when a status change is meaningful for analytics.
_STATUS_AUDIT_ACTION = {
    "no_show": "booking_no_show",
    "completed": "booking_completed",
    "cancelled": "booking_cancelled",
}


@router.get("", response_model=BookingListResponse)
async def list_bookings(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    _user: User = Depends(require_permission(VIEW_APPOINTMENTS)),
) -> BookingListResponse:
    today = datetime.now(UTC).date()
    start = from_date or today
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)

    base = (
        select(Booking)
        .where(Booking.practice_id == practice.id)
        .where(Booking.appointment_at >= start_dt)
    )
    if to_date is not None:
        # Only when somebody asked for it. The default used to stop thirty days
        # out, so an appointment the agent booked for November simply was not on
        # the screen in September — no empty state, no note about a range, just
        # a shorter list. The clinic reasonably concluded the booking had been
        # lost. A practice should see every appointment it has.
        base = base.where(
            Booking.appointment_at
            <= datetime.combine(to_date, datetime.max.time(), tzinfo=UTC)
        )
    if status:
        base = base.where(Booking.status == status)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    rows = (
        await db.execute(
            base.order_by(Booking.appointment_at.asc()).limit(limit).offset(offset)
        )
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
                patient_name=_full_name(patient),
                patient_phone=patient.phone if patient else None,
                in_pms=bool(b.pms_external_id),
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

    return BookingListResponse(
        bookings=summaries,
        total=total,
        has_more=(offset + len(summaries)) < total,
    )


@router.get("/export")
async def export_bookings(
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    status: str | None = Query(default=None),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    _user: User = Depends(require_permission(VIEW_APPOINTMENTS)),
) -> StreamingResponse:
    """Export bookings as CSV. Returns all matching bookings (no pagination)."""
    today = datetime.now(UTC).date()
    start = from_date or today
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)

    base = (
        select(Booking)
        .where(Booking.practice_id == practice.id)
        .where(Booking.appointment_at >= start_dt)
    )
    if to_date is not None:
        # Only when somebody asked for it. The default used to stop thirty days
        # out, so an appointment the agent booked for November simply was not on
        # the screen in September — no empty state, no note about a range, just
        # a shorter list. The clinic reasonably concluded the booking had been
        # lost. A practice should see every appointment it has.
        base = base.where(
            Booking.appointment_at
            <= datetime.combine(to_date, datetime.max.time(), tzinfo=UTC)
        )
    if status:
        base = base.where(Booking.status == status)

    rows = (
        await db.execute(base.order_by(Booking.appointment_at.asc()))
    ).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    # HIPAA: не экспортируем полные имена, только redacted
    writer.writerow([
        "ID", "Patient", "Appointment Date", "Duration (min)",
        "Procedure", "Provider", "Status", "Source", "Created At"
    ])
    for b in rows:
        patient = (
            await db.execute(select(Patient).where(Patient.id == b.patient_id))
        ).scalar_one_or_none()
        name = redact_name(patient.first_name, patient.last_name) if patient else "—"
        writer.writerow([
            str(b.id),
            name,
            b.appointment_at.strftime("%Y-%m-%d %H:%M") if b.appointment_at else "",
            b.duration_minutes,
            b.procedure_type,
            b.provider_name or "—",
            b.status,
            b.source or "—",
            b.created_at.strftime("%Y-%m-%d") if b.created_at else "",
        ])

    output.seek(0)
    filename = (
        f"bookings_{start.isoformat()}_"
        f"{to_date.isoformat() if to_date else 'onwards'}.csv"
    )
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/{booking_id}", response_model=BookingSummary)
async def get_booking(
    booking_id: str,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    _user: User = Depends(require_permission(VIEW_APPOINTMENTS)),
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
        patient_name=_full_name(patient),
        patient_phone=patient.phone if patient else None,
        in_pms=bool(b.pms_external_id),
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


@router.patch("/{booking_id}/status", response_model=BookingSummary)
async def update_booking_status(
    booking_id: str,
    payload: BookingStatusUpdate,
    practice: Practice = Depends(get_current_practice),
    # RBAC: changing an appointment's status is a scheduling action → staff+
    # (viewers are read-only). Enforced server-side regardless of the UI.
    _: User = Depends(require_permission(MANAGE_APPOINTMENTS)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BookingSummary:
    """Update a booking's lifecycle status from the dashboard.

    Primary use: marking a patient as a no-show after a missed appointment, so
    the no-show rate (analytics) and per-patient no-show count stay accurate.
    Meaningful transitions write an audit log so the analytics activity feed
    picks them up.
    """
    if payload.status not in _ALLOWED_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {sorted(_ALLOWED_STATUSES)}",
        )

    b = (
        await db.execute(
            select(Booking).where(
                Booking.id == booking_id,
                Booking.practice_id == practice.id,
            )
        )
    ).scalar_one_or_none()
    if b is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Booking not found.",
        )

    previous = b.status
    b.status = payload.status

    # Only audit a real transition into an analytics-relevant state.
    audit_action = _STATUS_AUDIT_ACTION.get(payload.status)
    if audit_action and previous != payload.status:
        db.add(
            AuditLog(
                id=uuid.uuid4(),
                practice_id=practice.id,
                action=audit_action,
                resource_type="booking",
                resource_id=b.id,
                audit_metadata={
                    "previous_status": previous,
                    "appointment_at": b.appointment_at.isoformat(),
                },
            )
        )

    await db.commit()
    # Re-bind the tenant after commit: the connection may be recycled on commit,
    # and the GUC is connection-scoped — without this, the post-commit refresh +
    # patient read would run with no tenant and RLS would hide the rows.
    await set_tenant(db, practice.id)
    await db.refresh(b)

    # And free the chair in the practice's OWN calendar. Cancelling only here is
    # the loss this product is sold to prevent, inverted: the patient is gone,
    # the front desk still sees them booked, and the hour stays blocked against
    # everyone else. cancel_in_pms has existed since this route was written and
    # nothing called it.
    if payload.status == "cancelled" and previous != "cancelled":
        await apply_cancellation(db, practice.id, b)

    patient = (
        await db.execute(select(Patient).where(Patient.id == b.patient_id))
    ).scalar_one_or_none()
    name = redact_name(patient.first_name, patient.last_name) if patient else None

    return BookingSummary(
        id=str(b.id),
        patient_name_redacted=name,
        patient_name=_full_name(patient),
        patient_phone=patient.phone if patient else None,
        in_pms=bool(b.pms_external_id),
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


@router.patch("/{booking_id}", response_model=BookingSummary)
async def edit_booking(
    booking_id: str,
    payload: BookingEdit,
    practice: Practice = Depends(get_current_practice),
    _: User = Depends(require_permission(MANAGE_APPOINTMENTS)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BookingSummary:
    """Move or amend an appointment, here and in the practice's own calendar.

    The front desk rearranges the day constantly — a patient rings to come in an
    hour later, a crown prep needs the long chair. Until now the dashboard could
    only mark a booking cancelled or no-show, so every real change meant editing
    the practice software by hand and leaving our copy wrong, which is how the
    agent starts offering an hour that is no longer free.

    Order matters. We commit here first, then move it there: if the second half
    fails the clinic still holds the OLD slot, which their front desk can see and
    fix. The other order leaves a patient holding a time the clinic never had.
    """
    b = (
        await db.execute(
            select(Booking).where(
                Booking.id == booking_id,
                Booking.practice_id == practice.id,
            )
        )
    ).scalar_one_or_none()
    if b is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Booking not found."
        )
    if b.status == "cancelled":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="This appointment is cancelled. Book a new one instead.",
        )

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Nothing to change.",
        )

    before = {
        "appointment_at": b.appointment_at.isoformat(),
        "duration_minutes": b.duration_minutes,
        "procedure_type": b.procedure_type,
        "provider_name": b.provider_name,
    }

    when = fields.get("appointment_at") or b.appointment_at
    minutes = fields.get("duration_minutes") or b.duration_minutes
    moved = "appointment_at" in fields or "duration_minutes" in fields

    if moved and await slot_is_taken(
        db, practice.id, when=when, duration_minutes=minutes, excluding=b.id
    ):
        # The unique constraint catches an exact collision and cannot see an
        # overlap: a 60-minute prep moved onto the half hour before a cleaning
        # slides through it and puts two people in one chair.
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Another appointment already covers that time.",
        )

    for key, value in fields.items():
        setattr(b, key, value)

    db.add(AuditLog(
        id=uuid.uuid4(),
        practice_id=practice.id,
        action="booking_edited",
        resource_type="booking",
        resource_id=b.id,
        audit_metadata={"before": before, "changed": sorted(fields)},
    ))
    await db.commit()
    await set_tenant(db, practice.id)
    await db.refresh(b)

    if moved:
        await apply_move(db, practice.id, b)

    patient = (
        await db.execute(select(Patient).where(Patient.id == b.patient_id))
    ).scalar_one_or_none()
    name = redact_name(patient.first_name, patient.last_name) if patient else None
    return BookingSummary(
        id=str(b.id),
        patient_name_redacted=name,
        patient_name=_full_name(patient),
        patient_phone=patient.phone if patient else None,
        in_pms=bool(b.pms_external_id),
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
