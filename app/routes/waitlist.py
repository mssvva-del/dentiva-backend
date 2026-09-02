from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import MANAGE_APPOINTMENTS, require_permission
from app.dependencies import get_current_practice, get_tenant_db
from app.models.patient import Patient
from app.models.practice import Practice
from app.models.user import User
from app.models.waitlist_entry import WaitlistEntry
from app.schemas.waitlist import (
    WaitlistListResponse,
    WaitlistStatusUpdate,
    WaitlistSummary,
)
from app.utils.redact import redact_name

router = APIRouter(prefix="/api/waitlist", tags=["waitlist"])

_ALLOWED_STATUSES = {"waiting", "notified", "booked", "removed"}


def _phone_last4(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


def _summary(entry: WaitlistEntry, patient: Patient | None) -> WaitlistSummary:
    first_name = patient.first_name if patient else None
    phone = patient.phone if patient else None
    return WaitlistSummary(
        id=str(entry.id),
        call_id=str(entry.call_id) if entry.call_id else None,
        patient_name_redacted=redact_name(first_name, None),
        phone_last4=_phone_last4(phone),
        patient_name=" ".join(
            x for x in (first_name, patient.last_name if patient else None) if x
        ) or None,
        patient_phone=phone,
        procedure_type=entry.procedure_type,
        preferred_date=entry.preferred_date,
        preferred_time_window=entry.preferred_time_window,
        notes=entry.notes,
        status=entry.status,
        notified_at=entry.notified_at,
        created_at=entry.created_at,
    )


@router.get("", response_model=WaitlistListResponse)
async def list_waitlist(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> WaitlistListResponse:
    base = select(WaitlistEntry).where(WaitlistEntry.practice_id == practice.id)
    if status_filter:
        base = base.where(WaitlistEntry.status == status_filter)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    waiting = (
        await db.execute(
            select(func.count())
            .select_from(WaitlistEntry)
            .where(
                WaitlistEntry.practice_id == practice.id,
                WaitlistEntry.status == "waiting",
            )
        )
    ).scalar_one()

    # Still-waiting first, then oldest first (longest wait bubbles up).
    rows = (
        await db.execute(
            base.order_by(
                case((WaitlistEntry.status == "waiting", 0), else_=1),
                WaitlistEntry.created_at.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    # Resolve patient PII (encrypted) per entry within the tenant.
    patients: dict = {}
    if rows:
        patient_ids = {r.patient_id for r in rows}
        for p in (
            await db.execute(select(Patient).where(Patient.id.in_(patient_ids)))
        ).scalars():
            patients[p.id] = p

    summaries = [_summary(r, patients.get(r.patient_id)) for r in rows]

    return WaitlistListResponse(
        entries=summaries,
        total=total,
        has_more=(offset + len(rows)) < total,
        waiting=waiting,
    )


@router.patch("/{entry_id}", response_model=WaitlistSummary)
async def update_waitlist_status(
    entry_id: str,
    payload: WaitlistStatusUpdate,
    practice: Practice = Depends(get_current_practice),
    # RBAC: working the waitlist (contacted/scheduled/removed) is a scheduling
    # action → staff+.
    _: User = Depends(require_permission(MANAGE_APPOINTMENTS)),
    db: AsyncSession = Depends(get_tenant_db),
) -> WaitlistSummary:
    if payload.status not in _ALLOWED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {sorted(_ALLOWED_STATUSES)}",
        )

    entry = (
        await db.execute(
            select(WaitlistEntry).where(
                WaitlistEntry.id == entry_id,
                WaitlistEntry.practice_id == practice.id,
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Waitlist entry not found."
        )

    entry.status = payload.status
    await db.commit()
    await db.refresh(entry)

    patient = (
        await db.execute(select(Patient).where(Patient.id == entry.patient_id))
    ).scalar_one_or_none()
    return _summary(entry, patient)
