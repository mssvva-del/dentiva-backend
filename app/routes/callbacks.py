from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_practice, get_tenant_db
from app.models.callback_request import CallbackRequest
from app.models.practice import Practice
from app.schemas.callback import (
    CallbackListResponse,
    CallbackStatusUpdate,
    CallbackSummary,
)
from app.utils.redact import redact_name

router = APIRouter(prefix="/api/callbacks", tags=["callbacks"])

_ALLOWED_STATUSES = {"pending", "handled", "dismissed"}


def _phone_last4(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else None


@router.get("", response_model=CallbackListResponse)
async def list_callbacks(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> CallbackListResponse:
    base = select(CallbackRequest).where(CallbackRequest.practice_id == practice.id)
    if status_filter:
        base = base.where(CallbackRequest.status == status_filter)

    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()

    # Pending urgent callbacks need attention now — count them for the badge.
    pending_urgent = (
        await db.execute(
            select(func.count())
            .select_from(CallbackRequest)
            .where(
                CallbackRequest.practice_id == practice.id,
                CallbackRequest.status == "pending",
                CallbackRequest.urgent.is_(True),
            )
        )
    ).scalar_one()

    # Urgent first, then newest first. Pending also bubbles above handled.
    rows = (
        await db.execute(
            base.order_by(
                case((CallbackRequest.status == "pending", 0), else_=1),
                CallbackRequest.urgent.desc(),
                CallbackRequest.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()

    summaries = [
        CallbackSummary(
            id=str(cb.id),
            call_id=str(cb.call_id) if cb.call_id else None,
            patient_name_redacted=redact_name(cb.patient_first_name, None),
            phone_last4=_phone_last4(cb.phone),
            reason=cb.reason,
            urgent=cb.urgent,
            status=cb.status,
            created_at=cb.created_at,
        )
        for cb in rows
    ]

    return CallbackListResponse(
        callbacks=summaries,
        total=total,
        has_more=(offset + len(rows)) < total,
        pending_urgent=pending_urgent,
    )


@router.patch("/{callback_id}", response_model=CallbackSummary)
async def update_callback_status(
    callback_id: str,
    payload: CallbackStatusUpdate,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
) -> CallbackSummary:
    if payload.status not in _ALLOWED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {sorted(_ALLOWED_STATUSES)}",
        )

    cb = (
        await db.execute(
            select(CallbackRequest).where(
                CallbackRequest.id == callback_id,
                CallbackRequest.practice_id == practice.id,
            )
        )
    ).scalar_one_or_none()
    if cb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Callback not found."
        )

    cb.status = payload.status
    await db.commit()
    await db.refresh(cb)

    return CallbackSummary(
        id=str(cb.id),
        call_id=str(cb.call_id) if cb.call_id else None,
        patient_name_redacted=redact_name(cb.patient_first_name, None),
        phone_last4=_phone_last4(cb.phone),
        reason=cb.reason,
        urgent=cb.urgent,
        status=cb.status,
        created_at=cb.created_at,
    )
