"""Clinic-facing billing API (Platform Iter 1, Phase D).

  GET  /api/billing/plans     → the plan catalog (for the picker)  [VIEW_BILLING]
  GET  /api/billing/summary   → current plan + usage + invoices    [VIEW_BILLING]
  POST /api/billing/checkout  → Stripe Checkout session URL        [MANAGE_BILLING]

VIEW_BILLING is manager+; MANAGE_BILLING (starting checkout) is owner-only — so a
manager can SEE billing but only the owner can change the plan. The admin panel
(Phase E) handles cross-clinic billing through a separate, audited path.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import MANAGE_BILLING, VIEW_BILLING, require_permission
from app.billing.metering import current_period
from app.billing.plans import PLANS, compute_overage_cents, get_plan
from app.config import get_settings
from app.dependencies import get_current_practice, get_tenant_db
from app.models.invoice import Invoice
from app.models.practice import Practice
from app.models.subscription import Subscription
from app.models.usage_record import UsageRecord
from app.models.user import User
from app.schemas.billing import (
    BillingSummary,
    CheckoutRequest,
    CheckoutResponse,
    InvoiceOut,
    PlanOut,
    UsageOut,
)
from app.services.stripe_client import BillingNotConfigured, create_checkout_session

router = APIRouter(prefix="/api/billing", tags=["billing"])


def _plan_out(key: str) -> PlanOut:
    p = PLANS[key]
    return PlanOut(
        key=p.key, name=p.name, monthly_price_cents=p.monthly_price_cents,
        annual_total_cents=p.annual_total_cents,
        annual_monthly_equivalent_cents=p.annual_monthly_equivalent_cents,
        included_minutes=p.included_minutes,
        overage_cents_per_min=p.overage_cents_per_min, per_location=p.per_location,
    )


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    _: User = Depends(require_permission(VIEW_BILLING)),
) -> list[PlanOut]:
    # Catalog order: cheapest first (dict insertion order is preserved).
    return [_plan_out(k) for k in PLANS]


@router.get("/summary", response_model=BillingSummary)
async def billing_summary(
    practice: Practice = Depends(get_current_practice),
    _: User = Depends(require_permission(VIEW_BILLING)),
    db: AsyncSession = Depends(get_tenant_db),
) -> BillingSummary:
    sub = (
        await db.execute(
            select(Subscription).where(Subscription.practice_id == practice.id)
        )
    ).scalar_one_or_none()

    # Current-period usage (the row metering writes to). May be absent early in
    # the period — show zeros against the entitlement.
    now = datetime.now(UTC)
    period_start, period_end = await current_period(db, practice.id, now=now)
    usage_row = (
        await db.execute(
            select(UsageRecord).where(
                UsageRecord.practice_id == practice.id,
                UsageRecord.period_start == period_start,
            )
        )
    ).scalar_one_or_none()

    included = sub.included_minutes if sub else (get_plan("after_hours").included_minutes)
    overage_rate = (
        (sub.overage_cents_per_min if sub and sub.overage_cents_per_min is not None
         else get_plan(sub.plan).overage_cents_per_min) if sub
        else get_plan("after_hours").overage_cents_per_min
    )
    minutes_used = float(usage_row.minutes_used) if usage_row else 0.0
    overage_cents = compute_overage_cents(minutes_used, included, overage_rate)

    usage = UsageOut(
        period_start=period_start, period_end=period_end,
        minutes_used=minutes_used, included_minutes=included,
        calls_count=usage_row.calls_count if usage_row else 0,
        sms_count=usage_row.sms_count if usage_row else 0,
        overage_minutes=max(0.0, minutes_used - included),
        overage_cents=overage_cents,
    )

    invoices = (
        await db.execute(
            select(Invoice).where(Invoice.practice_id == practice.id)
            .order_by(Invoice.created_at.desc()).limit(12)
        )
    ).scalars().all()

    return BillingSummary(
        plan=sub.plan if sub else None,
        plan_name=(get_plan(sub.plan).name if sub and get_plan(sub.plan) else None),
        status=sub.status if sub else None,
        billing_cycle=sub.billing_cycle if sub else None,
        mrr_cents=sub.mrr_cents if sub else 0,
        included_minutes=included,
        usage=usage,
        invoices=[
            InvoiceOut(
                id=str(i.id), amount_cents=i.amount_cents, status=i.status,
                period_start=i.period_start, period_end=i.period_end,
                paid_at=i.paid_at, created_at=i.created_at,
                hosted_invoice_url=i.hosted_invoice_url,
                invoice_pdf_url=i.invoice_pdf_url,
            )
            for i in invoices
        ],
    )


@router.post("/checkout", response_model=CheckoutResponse)
async def start_checkout(
    payload: CheckoutRequest,
    practice: Practice = Depends(get_current_practice),
    user: User = Depends(require_permission(MANAGE_BILLING)),
) -> CheckoutResponse:
    if get_plan(payload.plan) is None or payload.billing_cycle not in ("monthly", "annual"):
        raise HTTPException(status_code=422, detail="Unknown plan or billing cycle.")
    settings = get_settings()
    if payload.plan == "growth" and not (
        settings.retell_outbound_agent_id and settings.retell_from_number
    ):
        # Growth is the tier whose whole pitch is outbound — reactivation,
        # recalls, confirmations. The engine is built, but until an outbound
        # agent and number are configured in this deployment, no outbound call
        # can be dialled. Selling the tier in that state is charging for a
        # capability the product cannot deliver, discovered by the clinic as
        # a silent nothing. The gate lifts itself the day the env is set.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The Growth plan includes outbound campaigns, which aren't "
                   "enabled on this deployment yet. Choose Full-Time, or "
                   "contact us to turn outbound on first.",
        )
    base = settings.dashboard_base_url or "http://localhost:3000"
    try:
        url = await create_checkout_session(
            practice_id=str(practice.id),
            plan_key=payload.plan,
            billing_cycle=payload.billing_cycle,
            success_url=f"{base}/settings/billing?checkout=success",
            cancel_url=f"{base}/settings/billing?checkout=cancelled",
            customer_email=user.email,
        )
    except BillingNotConfigured as exc:
        # Keys/prices not wired yet (Iter 1 default) — clean, explicit signal.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured yet. Your Dentiva contact will set this up.",
        ) from exc
    return CheckoutResponse(url=url)
