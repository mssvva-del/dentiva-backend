"""Patient routes."""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_practice, get_tenant_db
from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.patient import Patient
from app.models.practice import Practice
from app.services.sms import send_recall_notice
from app.utils.redact import redact_name

logger = logging.getLogger("dentiva.routes.patients")

router = APIRouter(prefix="/api/patients", tags=["patients"])


def _mask_phone(phone: str | None) -> str | None:
    """Return a HIPAA-safe masked phone like "(•••) •••-1234"."""
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return "•••"
    return f"(•••) •••-{digits[-4:]}"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class PatientSummary(BaseModel):
    patient_id: str
    name_redacted: str | None
    phone_masked: str | None
    last_visit_date: str | None      # ISO date or null
    next_visit_date: str | None      # ISO date or null
    total_visits: int
    no_show_count: int = 0           # missed appointments (repeat-offender flag)
    status: str                      # new | upcoming | recall_due | active


class PatientsListResponse(BaseModel):
    patients: list[PatientSummary]
    total: int
    has_more: bool


@router.get("", response_model=PatientsListResponse)
async def list_patients(
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> PatientsListResponse:
    """Patient roster with per-patient visit aggregates.

    Names/phones are encrypted at rest, so search and aggregation happen in
    Python (the practice roster is small). Names are redacted to "First L."
    and phones masked to the last 4 digits before leaving the backend.
    """
    now = datetime.now(UTC)
    recall_cutoff = now - timedelta(days=6 * 30)

    patients = (
        await db.execute(select(Patient).where(Patient.practice_id == practice.id))
    ).scalars().all()

    bookings = (
        await db.execute(select(Booking).where(Booking.practice_id == practice.id))
    ).scalars().all()

    by_patient: dict[object, list[Booking]] = defaultdict(list)
    for b in bookings:
        by_patient[b.patient_id].append(b)

    rows: list[PatientSummary] = []
    for p in patients:
        bks = by_patient.get(p.id, [])
        completed = [b for b in bks if b.status == "completed"]
        no_shows = [b for b in bks if b.status == "no_show"]
        upcoming = [
            b
            for b in bks
            if b.status in ("confirmed", "completed") and _aware(b.appointment_at) >= now
        ]
        last_visit = max((_aware(b.appointment_at) for b in completed), default=None)
        next_visit = min((_aware(b.appointment_at) for b in upcoming), default=None)

        if not completed and not upcoming:
            status = "new"
        elif upcoming:
            status = "upcoming"
        elif last_visit is not None and last_visit < recall_cutoff:
            status = "recall_due"
        else:
            status = "active"

        rows.append(
            PatientSummary(
                patient_id=str(p.id),
                name_redacted=redact_name(p.first_name, p.last_name),
                phone_masked=_mask_phone(p.phone),
                last_visit_date=last_visit.date().isoformat() if last_visit else None,
                next_visit_date=next_visit.date().isoformat() if next_visit else None,
                total_visits=len(completed),
                no_show_count=len(no_shows),
                status=status,
            )
        )

    if search:
        needle = search.strip().lower()
        rows = [r for r in rows if r.name_redacted and needle in r.name_redacted.lower()]

    # Most recent activity first; patients with no visit date sink to the end.
    def _sort_key(r: PatientSummary) -> tuple[int, str]:
        recent = r.next_visit_date or r.last_visit_date or ""
        return (1 if recent else 0, recent)

    rows.sort(key=_sort_key, reverse=True)

    total = len(rows)
    page = rows[offset : offset + limit]
    return PatientsListResponse(
        patients=page,
        total=total,
        has_more=offset + limit < total,
    )


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


class RecallSmsResponse(BaseModel):
    sent: bool
    status: str  # sent | skipped | error
    detail: str | None = None


@router.post("/{patient_id}/recall-sms", response_model=RecallSmsResponse)
async def send_patient_recall_sms(
    patient_id: str,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> RecallSmsResponse:
    """Text a lapsed patient a reactivation / recall message.

    Staff trigger this from the Reactivation list. Phone (encrypted at rest) is
    decrypted server-side and never returned. Sending is fail-safe: if SMS is
    disabled or the send fails, we still return 200 with the outcome and log the
    staff action so the reactivation effort is auditable.
    """
    patient = (
        await db.execute(
            select(Patient).where(
                Patient.id == patient_id,
                Patient.practice_id == practice.id,
            )
        )
    ).scalar_one_or_none()
    if patient is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Patient not found.",
        )

    result = await send_recall_notice(
        to=patient.phone,
        practice_name=practice.name,
        first_name=patient.first_name,
    )

    if result.get("sent"):
        status_label = "sent"
    elif "skipped" in result:
        status_label = "skipped"
    else:
        status_label = "error"

    db.add(
        AuditLog(
            id=uuid.uuid4(),
            practice_id=practice.id,
            action="recall_sms_sent",
            resource_type="patient",
            resource_id=patient.id,
            audit_metadata={"outcome": status_label},
        )
    )
    await db.commit()

    logger.info("recall-sms: patient=%s outcome=%s", patient_id, status_label)
    return RecallSmsResponse(
        sent=bool(result.get("sent")),
        status=status_label,
        detail=result.get("skipped") or result.get("detail"),
    )
