"""Patient routes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_practice, get_tenant_db
from app.models.booking import Booking
from app.models.patient import Patient
from app.models.practice import Practice
from app.utils.redact import redact_name

router = APIRouter(prefix="/api/patients", tags=["patients"])


class RecallPatient(BaseModel):
    patient_id: str
    patient_name_redacted: str | None
    last_visit_date: str          # ISO date string (date only)
    last_procedure: str | None
    months_since_visit: int


class RecallResponse(BaseModel):
    patients: list[RecallPatient]
    total: int
    recall_threshold_months: int


@router.get("/recall", response_model=RecallResponse)
async def get_recall_patients(
    threshold_months: int = Query(default=6, ge=1, le=24),
    limit: int = Query(default=20, ge=1, le=100),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> RecallResponse:
    """Return patients who haven't had an appointment in threshold_months months
    and have no upcoming appointment in the next 30 days."""
    now = datetime.now(UTC)
    cutoff_past = now - timedelta(days=threshold_months * 30)
    cutoff_future = now + timedelta(days=30)

    # Patients with a booking OLDER than cutoff_past
    past_q = (
        select(
            Booking.patient_id,
            func.max(Booking.appointment_at).label("last_visit"),
            func.max(Booking.procedure_type).label("last_procedure"),
        )
        .where(
            Booking.practice_id == practice.id,
            Booking.status == "completed",
            Booking.appointment_at < cutoff_past,
        )
        .group_by(Booking.patient_id)
        .subquery()
    )

    # Patients WITH upcoming bookings (to exclude)
    future_q = (
        select(Booking.patient_id)
        .where(
            Booking.practice_id == practice.id,
            Booking.status.in_(["confirmed", "completed"]),
            Booking.appointment_at >= now,
            Booking.appointment_at <= cutoff_future,
        )
        .distinct()
        .subquery()
    )

    # Final: past patients NOT in upcoming
    stmt = (
        select(past_q, Patient)
        .join(Patient, Patient.id == past_q.c.patient_id)
        .where(past_q.c.patient_id.notin_(select(future_q.c.patient_id)))
        .order_by(past_q.c.last_visit.asc())  # oldest first (most overdue)
        .limit(limit)
    )

    rows = (await db.execute(stmt)).all()

    patients_out: list[RecallPatient] = []
    for row in rows:
        last_visit_dt = row.last_visit
        if last_visit_dt.tzinfo is None:
            last_visit_dt = last_visit_dt.replace(tzinfo=UTC)
        months = int((now - last_visit_dt).days / 30)
        name = redact_name(row.Patient.first_name, row.Patient.last_name)
        patients_out.append(
            RecallPatient(
                patient_id=str(row.patient_id),
                patient_name_redacted=name,
                last_visit_date=last_visit_dt.date().isoformat(),
                last_procedure=row.last_procedure,
                months_since_visit=months,
            )
        )

    # Total count (without limit)
    total_q = (
        select(func.count())
        .select_from(
            select(past_q.c.patient_id)
            .where(past_q.c.patient_id.notin_(select(future_q.c.patient_id)))
            .subquery()
        )
    )
    total = (await db.execute(total_q)).scalar_one()

    return RecallResponse(
        patients=patients_out,
        total=total,
        recall_threshold_months=threshold_months,
    )
