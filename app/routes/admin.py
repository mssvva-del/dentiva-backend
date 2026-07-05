"""Dentiva ADMIN API (Platform Iter 1, Phase E).

Cross-tenant operations for internal Dentiva staff. EVERY endpoint is gated by a
Dentiva-role permission (deny-by-default via require_admin_permission) and lives
under /api/admin. This is the "separate, audited code path" the spec mandates —
clinic users (is_internal=False) can never reach it.

AUDIT: any endpoint that reads a specific clinic's data or mutates state writes an
audit_logs row (actor = the admin user). PHI itself is NOT exposed here — clinic
detail shows non-PHI aggregates (counts, billing) so we never decrypt patient data
in the admin world.

Cross-tenant reads: billing/practices/users tables are non-RLS, so the normal app
session reads across clinics directly. (PHI tables stay RLS-protected and are not
queried here beyond COUNT, which RLS would block — so counts are done with a
privileged COUNT via the practice filter on non-RLS join tables where possible.)
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db as _app_db
from app.auth.admin import AdminContext, require_admin_permission
from app.auth.permissions import (
    IMPERSONATE_CLINIC,
    MANAGE_BILLING_ALL,
    MANAGE_DENTIVA_STAFF,
    MANAGE_FEATURE_FLAGS,
    MANAGE_LEADS,
    MANAGE_PRICING,
    MANAGE_SUBSCRIPTIONS,
    VIEW_ALL_CLINICS,
    VIEW_AUDIT_LOGS,
    VIEW_BILLING_ALL,
    VIEW_CLINIC_DETAIL,
    VIEW_REVENUE,
    VIEW_SYSTEM_HEALTH,
    VIEW_USAGE_METRICS,
)
from app.billing.plans import ESTIMATED_COST_CENTS_PER_MIN, get_plan
from app.models.audit_log import AuditLog
from app.models.booking import Booking
from app.models.call import Call
from app.models.dentiva_staff import DentivaStaff
from app.models.feature_flag import FeatureFlag
from app.models.lead import Lead
from app.models.practice import Practice
from app.models.pricing_plan import PricingPlan
from app.models.subscription import Subscription
from app.models.usage_record import UsageRecord
from app.models.user import User

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Audit helper — admin actions are always logged (actor = the internal user).
# ---------------------------------------------------------------------------
async def _audit(
    session: AsyncSession, ctx: AdminContext, action: str,
    *, practice_id: uuid.UUID | None = None, meta: dict | None = None,
) -> None:
    session.add(AuditLog(
        id=uuid.uuid4(),
        # practice_id is required on audit_logs; use a zero-uuid for non-clinic
        # admin actions (staff/flags) so the column stays populated.
        practice_id=practice_id or uuid.UUID(int=0),
        user_id=ctx.user.id,
        action=action,
        resource_type="admin",
        resource_id=practice_id,
        audit_metadata={**(meta or {}), "staff_role": ctx.staff_role},
    ))


# ===========================================================================
# 1–2. Clinics: list + detail
# ===========================================================================
class ClinicRow(BaseModel):
    id: str
    name: str
    status: str
    plan: str | None
    mrr_cents: int
    onboarding_step: int
    created_at: datetime


@router.get("/clinics", response_model=list[ClinicRow])
async def list_clinics(
    ctx: AdminContext = Depends(require_admin_permission(VIEW_ALL_CLINICS)),
) -> list[ClinicRow]:
    async with _app_db.async_session_factory() as session:
        rows = (
            await session.execute(select(Practice).order_by(Practice.created_at.desc()))
        ).scalars().all()
        subs = {
            s.practice_id: s
            for s in (await session.execute(select(Subscription))).scalars().all()
        }
    return [
        ClinicRow(
            id=str(p.id), name=p.name, status=p.status,
            plan=subs[p.id].plan if p.id in subs else None,
            mrr_cents=subs[p.id].mrr_cents if p.id in subs else 0,
            onboarding_step=p.onboarding_step, created_at=p.created_at,
        )
        for p in rows
    ]


class ClinicDetail(BaseModel):
    id: str
    name: str
    status: str
    timezone: str
    pms_system: str
    languages_enabled: list[str]
    plan: str | None
    subscription_status: str | None
    included_minutes: int | None
    mrr_cents: int
    user_count: int
    call_count: int
    booking_count: int


@router.get("/clinics/{practice_id}", response_model=ClinicDetail)
async def clinic_detail(
    practice_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_permission(VIEW_CLINIC_DETAIL)),
) -> ClinicDetail:
    async with _app_db.async_session_factory() as session:
        p = (
            await session.execute(select(Practice).where(Practice.id == practice_id))
        ).scalar_one_or_none()
        if p is None:
            raise HTTPException(status_code=404, detail="Clinic not found.")
        sub = (
            await session.execute(
                select(Subscription).where(Subscription.practice_id == practice_id)
            )
        ).scalar_one_or_none()
        users = (await session.execute(
            select(func.count()).select_from(User).where(User.practice_id == practice_id)
        )).scalar_one()
        # PHI tables (calls) are RLS-protected; bind the tenant just for these
        # COUNTs (no PHI columns selected). This read is audited below.
        from app.db import set_tenant
        await set_tenant(session, practice_id)
        calls = (await session.execute(
            select(func.count()).select_from(Call).where(Call.practice_id == practice_id)
        )).scalar_one()
        bookings = (await session.execute(
            select(func.count()).select_from(Booking).where(Booking.practice_id == practice_id)
        )).scalar_one()
        await _audit(session, ctx, "admin_view_clinic", practice_id=practice_id)
        await session.commit()

    return ClinicDetail(
        id=str(p.id), name=p.name, status=p.status, timezone=p.timezone,
        pms_system=p.pms_system, languages_enabled=list(p.languages_enabled),
        plan=sub.plan if sub else None,
        subscription_status=sub.status if sub else None,
        included_minutes=sub.included_minutes if sub else None,
        mrr_cents=sub.mrr_cents if sub else 0,
        user_count=users, call_count=calls, booking_count=bookings,
    )


# ===========================================================================
# 3. Impersonation — server records intent + audit; UI shows a banner.
# ===========================================================================
class ImpersonateResponse(BaseModel):
    practice_id: str
    practice_name: str
    granted_at: datetime
    note: str


@router.post("/clinics/{practice_id}/impersonate", response_model=ImpersonateResponse)
async def impersonate(
    practice_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_permission(IMPERSONATE_CLINIC)),
) -> ImpersonateResponse:
    """Begin impersonating a clinic (audited). Returns the target context the UI
    uses to show a persistent 'Viewing as <clinic>' banner. The actual data view
    is read-only and every impersonation start is logged for HIPAA."""
    async with _app_db.async_session_factory() as session:
        p = (
            await session.execute(select(Practice).where(Practice.id == practice_id))
        ).scalar_one_or_none()
        if p is None:
            raise HTTPException(status_code=404, detail="Clinic not found.")
        await _audit(session, ctx, "admin_impersonate_start", practice_id=practice_id,
                     meta={"clinic": p.name})
        await session.commit()
        name = p.name
    return ImpersonateResponse(
        practice_id=str(practice_id), practice_name=name,
        granted_at=datetime.now(UTC),
        note="Read-only impersonation. This access is logged.",
    )


# ===========================================================================
# 4–5. Subscriptions & billing (override / pilot)
# ===========================================================================
class SubscriptionRow(BaseModel):
    practice_id: str
    practice_name: str
    plan: str
    status: str
    billing_cycle: str
    included_minutes: int
    mrr_cents: int


@router.get("/billing/subscriptions", response_model=list[SubscriptionRow])
async def list_subscriptions(
    ctx: AdminContext = Depends(require_admin_permission(VIEW_USAGE_METRICS)),
) -> list[SubscriptionRow]:
    async with _app_db.async_session_factory() as session:
        subs = (await session.execute(select(Subscription))).scalars().all()
        names = {
            p.id: p.name for p in (await session.execute(select(Practice))).scalars().all()
        }
    return [
        SubscriptionRow(
            practice_id=str(s.practice_id), practice_name=names.get(s.practice_id, "—"),
            plan=s.plan, status=s.status, billing_cycle=s.billing_cycle,
            included_minutes=s.included_minutes, mrr_cents=s.mrr_cents,
        )
        for s in subs
    ]


class SubscriptionOverride(BaseModel):
    plan: str | None = None
    status: str | None = None            # active|pilot|suspended|cancelled|past_due
    included_minutes: int | None = None
    mrr_cents: int | None = None
    overage_cents_per_min: int | None = None
    setup_fee_cents: int | None = None


@router.patch("/clinics/{practice_id}/subscription", response_model=SubscriptionRow)
async def override_subscription(
    practice_id: uuid.UUID,
    payload: SubscriptionOverride,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_SUBSCRIPTIONS)),
) -> SubscriptionRow:
    """Per-client override of plan/price/minutes/status (custom deals + pilots).
    Creates a subscription if none exists. Fully audited."""
    async with _app_db.async_session_factory() as session:
        p = (
            await session.execute(select(Practice).where(Practice.id == practice_id))
        ).scalar_one_or_none()
        if p is None:
            raise HTTPException(status_code=404, detail="Clinic not found.")
        sub = (
            await session.execute(
                select(Subscription).where(Subscription.practice_id == practice_id)
            )
        ).scalar_one_or_none()
        if sub is None:
            plan_key = payload.plan or "starter"
            if get_plan(plan_key) is None:
                raise HTTPException(status_code=422, detail="Unknown plan.")
            plan = get_plan(plan_key)
            sub = Subscription(
                id=uuid.uuid4(), practice_id=practice_id, plan=plan_key,
                included_minutes=plan.included_minutes, mrr_cents=plan.monthly_price_cents,
            )
            session.add(sub)

        changed: dict = {}
        for field in ("plan", "status", "included_minutes", "mrr_cents",
                      "overage_cents_per_min", "setup_fee_cents"):
            val = getattr(payload, field)
            if val is not None:
                if field == "plan" and get_plan(val) is None:
                    raise HTTPException(status_code=422, detail="Unknown plan.")
                setattr(sub, field, val)
                changed[field] = val
        # Keep the practice lifecycle in step with a pilot/suspend override.
        if payload.status == "pilot":
            p.status = "pilot"
        elif payload.status == "suspended":
            p.status = "suspended"
        elif payload.status == "active":
            p.status = "active"

        await _audit(session, ctx, "admin_override_subscription",
                     practice_id=practice_id, meta=changed)
        await session.commit()
        await session.refresh(sub)
        row = SubscriptionRow(
            practice_id=str(sub.practice_id), practice_name=p.name, plan=sub.plan,
            status=sub.status, billing_cycle=sub.billing_cycle,
            included_minutes=sub.included_minutes, mrr_cents=sub.mrr_cents,
        )
    return row


# ===========================================================================
# 6–7. Usage / cost / margin + revenue rollups
# ===========================================================================
class RevenueSummary(BaseModel):
    total_mrr_cents: int
    active_clinics: int
    pilot_clinics: int
    suspended_clinics: int
    # Margin estimate over current-period metered minutes (planning only).
    period_minutes: float
    estimated_cost_cents: int
    estimated_margin_cents: int


@router.get("/revenue", response_model=RevenueSummary)
async def revenue(
    ctx: AdminContext = Depends(require_admin_permission(VIEW_REVENUE)),
) -> RevenueSummary:
    async with _app_db.async_session_factory() as session:
        subs = (await session.execute(select(Subscription))).scalars().all()
        practices = (await session.execute(select(Practice))).scalars().all()
        minutes = (await session.execute(
            select(func.coalesce(func.sum(UsageRecord.minutes_used), 0))
        )).scalar_one()
    total_mrr = sum(s.mrr_cents for s in subs if s.status in ("active", "trialing"))
    minutes_f = float(minutes)
    cost = round(minutes_f * ESTIMATED_COST_CENTS_PER_MIN)
    return RevenueSummary(
        total_mrr_cents=total_mrr,
        active_clinics=sum(1 for p in practices if p.status == "active"),
        pilot_clinics=sum(1 for p in practices if p.status == "pilot"),
        suspended_clinics=sum(1 for p in practices if p.status == "suspended"),
        period_minutes=minutes_f,
        estimated_cost_cents=cost,
        estimated_margin_cents=total_mrr - cost,
    )


# ===========================================================================
# 8. Dentiva staff management
# ===========================================================================
class StaffRow(BaseModel):
    user_id: str
    email: str
    role: str


@router.get("/staff", response_model=list[StaffRow])
async def list_staff(
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_DENTIVA_STAFF)),
) -> list[StaffRow]:
    async with _app_db.async_session_factory() as session:
        staff = (await session.execute(select(DentivaStaff))).scalars().all()
        users = {
            u.id: u for u in (await session.execute(select(User))).scalars().all()
        }
    return [
        StaffRow(user_id=str(s.user_id), email=users[s.user_id].email if s.user_id in users
                 else "—", role=s.role)
        for s in staff
    ]


class StaffRoleUpdate(BaseModel):
    role: str  # super_admin|support|sales|finance|engineer


@router.patch("/staff/{user_id}", response_model=StaffRow)
async def update_staff_role(
    user_id: uuid.UUID,
    payload: StaffRoleUpdate,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_DENTIVA_STAFF)),
) -> StaffRow:
    from app.auth.permissions import ADMIN_ROLE_PERMISSIONS
    if payload.role not in ADMIN_ROLE_PERMISSIONS:
        raise HTTPException(status_code=422, detail="Unknown Dentiva role.")
    async with _app_db.async_session_factory() as session:
        staff = (
            await session.execute(select(DentivaStaff).where(DentivaStaff.user_id == user_id))
        ).scalar_one_or_none()
        if staff is None:
            raise HTTPException(status_code=404, detail="Staff member not found.")
        if staff.role == "super_admin" and payload.role != "super_admin":
            # Lockout guard: demoting the LAST super_admin freezes staff management
            # (only super_admin holds MANAGE_DENTIVA_STAFF). Lock rows to serialize
            # concurrent demotions, then count — same guard as the delete endpoint.
            sa_rows = (await session.execute(
                select(DentivaStaff).where(DentivaStaff.role == "super_admin")
                .with_for_update()
            )).scalars().all()
            if len(sa_rows) <= 1:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot demote the last super_admin — promote someone "
                           "else first.",
                )
        staff.role = payload.role
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        await _audit(session, ctx, "admin_update_staff_role",
                     meta={"target": str(user_id), "role": payload.role})
        await session.commit()
        email = user.email if user else "—"
    return StaffRow(user_id=str(user_id), email=email, role=payload.role)


# ===========================================================================
# 9. System health
# ===========================================================================
class SystemHealth(BaseModel):
    db_ok: bool
    clinics: int
    internal_staff: int
    environment: str


@router.get("/system-health", response_model=SystemHealth)
async def system_health(
    ctx: AdminContext = Depends(require_admin_permission(VIEW_SYSTEM_HEALTH)),
) -> SystemHealth:
    from app.config import get_settings
    db_ok = True
    clinics = staff = 0
    try:
        async with _app_db.async_session_factory() as session:
            clinics = (await session.execute(
                select(func.count()).select_from(Practice)
            )).scalar_one()
            staff = (await session.execute(
                select(func.count()).select_from(DentivaStaff)
            )).scalar_one()
    except Exception:  # noqa: BLE001
        db_ok = False
    return SystemHealth(
        db_ok=db_ok, clinics=clinics, internal_staff=staff,
        environment=get_settings().environment,
    )


# ===========================================================================
# 10. Feature flags
# ===========================================================================
class FlagRow(BaseModel):
    id: str
    practice_id: str | None
    flag_key: str
    enabled: bool
    description: str | None


class FlagUpsert(BaseModel):
    flag_key: str
    enabled: bool
    practice_id: str | None = None
    description: str | None = None


@router.get("/feature-flags", response_model=list[FlagRow])
async def list_flags(
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_FEATURE_FLAGS)),
) -> list[FlagRow]:
    async with _app_db.async_session_factory() as session:
        flags = (
            await session.execute(select(FeatureFlag).order_by(FeatureFlag.flag_key))
        ).scalars().all()
    return [
        FlagRow(id=str(f.id), practice_id=str(f.practice_id) if f.practice_id else None,
                flag_key=f.flag_key, enabled=f.enabled, description=f.description)
        for f in flags
    ]


@router.put("/feature-flags", response_model=FlagRow)
async def upsert_flag(
    payload: FlagUpsert,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_FEATURE_FLAGS)),
) -> FlagRow:
    pid = uuid.UUID(payload.practice_id) if payload.practice_id else None
    async with _app_db.async_session_factory() as session:
        q = select(FeatureFlag).where(FeatureFlag.flag_key == payload.flag_key)
        q = q.where(FeatureFlag.practice_id == pid) if pid else q.where(
            FeatureFlag.practice_id.is_(None))
        flag = (await session.execute(q)).scalar_one_or_none()
        if flag is None:
            flag = FeatureFlag(id=uuid.uuid4(), flag_key=payload.flag_key, practice_id=pid)
            session.add(flag)
        flag.enabled = payload.enabled
        if payload.description is not None:
            flag.description = payload.description
        await _audit(session, ctx, "admin_set_feature_flag", practice_id=pid,
                     meta={"flag": payload.flag_key, "enabled": payload.enabled})
        await session.commit()
        await session.refresh(flag)
        row = FlagRow(id=str(flag.id),
                      practice_id=str(flag.practice_id) if flag.practice_id else None,
                      flag_key=flag.flag_key, enabled=flag.enabled,
                      description=flag.description)
    return row


# ===========================================================================
# 11. Audit log viewer
# ===========================================================================
class AuditRow(BaseModel):
    id: str
    practice_id: str | None
    user_id: str | None
    action: str
    resource_type: str
    created_at: datetime
    metadata: dict | None


@router.get("/audit-logs", response_model=list[AuditRow])
async def audit_logs(
    limit: int = 100,
    ctx: AdminContext = Depends(require_admin_permission(VIEW_AUDIT_LOGS)),
) -> list[AuditRow]:
    limit = max(1, min(limit, 500))
    async with _app_db.async_session_factory() as session:
        rows = (
            await session.execute(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
            )
        ).scalars().all()
    zero = uuid.UUID(int=0)
    return [
        AuditRow(
            id=str(a.id),
            practice_id=(str(a.practice_id) if a.practice_id and a.practice_id != zero
                         else None),
            user_id=str(a.user_id) if a.user_id else None,
            action=a.action, resource_type=a.resource_type,
            created_at=a.created_at, metadata=a.audit_metadata,
        )
        for a in rows
    ]


# ===========================================================================
# 10. Leads inbox (marketing-site demo form → sales)
# ===========================================================================
class LeadRow(BaseModel):
    id: str
    name: str | None
    email: str | None
    phone: str | None
    clinic_name: str | None
    message: str | None
    source: str
    status: str
    notes: str | None
    created_at: datetime


class LeadUpdate(BaseModel):
    status: str | None = None
    notes: str | None = None


_LEAD_STATUSES = frozenset({"new", "contacted", "qualified", "won", "lost"})


def _lead_row(lead: Lead) -> LeadRow:
    return LeadRow(
        id=str(lead.id), name=lead.name, email=lead.email, phone=lead.phone,
        clinic_name=lead.clinic_name, message=lead.message, source=lead.source,
        status=lead.status, notes=lead.notes, created_at=lead.created_at,
    )


@router.get("/leads", response_model=list[LeadRow])
async def list_leads(
    status: str | None = None,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_LEADS)),
) -> list[LeadRow]:
    """Sales lead inbox — newest first, optionally filtered by status."""
    async with _app_db.async_session_factory() as session:
        q = select(Lead)
        if status:
            q = q.where(Lead.status == status)
        rows = (await session.execute(
            q.order_by(Lead.created_at.desc()).limit(500)
        )).scalars().all()
    return [_lead_row(x) for x in rows]


@router.patch("/leads/{lead_id}", response_model=LeadRow)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdate,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_LEADS)),
) -> LeadRow:
    """Move a lead through the pipeline / add sales notes."""
    if payload.status is not None and payload.status not in _LEAD_STATUSES:
        raise HTTPException(status_code=422, detail="Unknown lead status.")
    async with _app_db.async_session_factory() as session:
        lead = (await session.execute(
            select(Lead).where(Lead.id == lead_id)
        )).scalar_one_or_none()
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found.")
        if payload.status is not None:
            lead.status = payload.status
        if payload.notes is not None:
            lead.notes = payload.notes
        await _audit(session, ctx, "admin_update_lead",
                     meta={"lead": str(lead_id), "status": lead.status})
        await session.commit()
        row = _lead_row(lead)
    return row


# ===========================================================================
# 11. Coupons / discounts (ADM2) — Stripe-native, applied to clinic subscriptions
# ===========================================================================
from app.models.invoice import Invoice  # noqa: E402 — grouped with its section
from app.services.stripe_client import (  # noqa: E402 — grouped with its section
    BillingNotConfigured,
    StripeError,
    apply_coupon_to_subscription,
    cancel_subscription,
    create_coupon,
    delete_coupon,
    list_coupons,
    refund_invoice,
    resume_subscription,
)


class CouponCreate(BaseModel):
    name: str
    percent_off: float | None = None       # 1–100
    amount_off_cents: int | None = None    # USD cents
    duration: str = "once"                 # once | repeating | forever
    duration_in_months: int | None = None  # required when duration == repeating


class CouponRow(BaseModel):
    id: str
    name: str | None
    percent_off: float | None
    amount_off_cents: int | None
    duration: str
    duration_in_months: int | None
    valid: bool


def _coupon_row(c: dict) -> CouponRow:
    return CouponRow(
        id=c.get("id", ""), name=c.get("name"),
        percent_off=c.get("percent_off"), amount_off_cents=c.get("amount_off"),
        duration=c.get("duration", "once"),
        duration_in_months=c.get("duration_in_months"),
        valid=bool(c.get("valid", True)),
    )


def _stripe_http(exc: Exception) -> HTTPException:
    if isinstance(exc, BillingNotConfigured):
        return HTTPException(status_code=503, detail="Billing is not configured yet.")
    # Upstream 5xx / transport failure → honest 502; Stripe 4xx → 422 (bad input).
    status = getattr(exc, "status_code", None) or 422
    return HTTPException(status_code=502 if status >= 500 else 422, detail=str(exc))


@router.get("/coupons", response_model=list[CouponRow])
async def admin_list_coupons(
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_SUBSCRIPTIONS)),
) -> list[CouponRow]:
    try:
        return [_coupon_row(c) for c in await list_coupons()]
    except (BillingNotConfigured, StripeError) as exc:
        raise _stripe_http(exc) from exc


@router.post("/coupons", response_model=CouponRow)
async def admin_create_coupon(
    payload: CouponCreate,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_SUBSCRIPTIONS)),
) -> CouponRow:
    # Exactly one discount kind; bounds enforced here so Stripe errors stay rare.
    if (payload.percent_off is None) == (payload.amount_off_cents is None):
        raise HTTPException(status_code=422,
                            detail="Provide exactly one of percent_off / amount_off_cents.")
    if payload.percent_off is not None and not (0 < payload.percent_off <= 100):
        raise HTTPException(status_code=422, detail="percent_off must be 1–100.")
    if payload.amount_off_cents is not None and payload.amount_off_cents <= 0:
        raise HTTPException(status_code=422, detail="amount_off_cents must be > 0.")
    if payload.duration not in ("once", "repeating", "forever"):
        raise HTTPException(status_code=422, detail="Unknown duration.")
    if payload.duration == "repeating" and (
        payload.duration_in_months is None or payload.duration_in_months <= 0
    ):
        raise HTTPException(status_code=422,
                            detail="duration_in_months must be > 0 for repeating.")
    try:
        coupon = await create_coupon(
            name=payload.name, percent_off=payload.percent_off,
            amount_off_cents=payload.amount_off_cents,
            duration=payload.duration, duration_in_months=payload.duration_in_months,
        )
    except (BillingNotConfigured, StripeError) as exc:
        raise _stripe_http(exc) from exc
    async with _app_db.async_session_factory() as session:
        await _audit(session, ctx, "admin_create_coupon",
                     meta={"coupon": coupon.get("id"), "name": payload.name})
        await session.commit()
    return _coupon_row(coupon)


@router.delete("/coupons/{coupon_id}")
async def admin_delete_coupon(
    coupon_id: str,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_SUBSCRIPTIONS)),
) -> dict:
    try:
        await delete_coupon(coupon_id)
    except (BillingNotConfigured, StripeError) as exc:
        raise _stripe_http(exc) from exc
    async with _app_db.async_session_factory() as session:
        await _audit(session, ctx, "admin_delete_coupon", meta={"coupon": coupon_id})
        await session.commit()
    return {"ok": True}


class ApplyCoupon(BaseModel):
    coupon_id: str


@router.post("/clinics/{practice_id}/apply-coupon")
async def admin_apply_coupon(
    practice_id: uuid.UUID,
    payload: ApplyCoupon,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_SUBSCRIPTIONS)),
) -> dict:
    """Attach a Stripe coupon to the clinic's subscription (discount from the next
    invoice). Clinics without a Stripe subscription (trials/pilots) get a clear 409 —
    use the existing subscription override for custom pricing there."""
    async with _app_db.async_session_factory() as session:
        sub = (await session.execute(
            select(Subscription).where(Subscription.practice_id == practice_id)
        )).scalar_one_or_none()
        if sub is None:
            raise HTTPException(status_code=404, detail="Clinic has no subscription.")
        if not sub.stripe_subscription_id:
            raise HTTPException(
                status_code=409,
                detail="Clinic has no Stripe subscription yet — use the subscription "
                       "override for custom pricing.",
            )
        try:
            await apply_coupon_to_subscription(sub.stripe_subscription_id, payload.coupon_id)
        except (BillingNotConfigured, StripeError) as exc:
            raise _stripe_http(exc) from exc
        await _audit(session, ctx, "admin_apply_coupon", practice_id=practice_id,
                     meta={"coupon": payload.coupon_id})
        await session.commit()
    return {"ok": True, "coupon": payload.coupon_id}


# ===========================================================================
# 12. Invoices + refunds (ADM3) — cancel/refund a clinic's payment
# ===========================================================================
class InvoiceRow(BaseModel):
    id: str
    stripe_invoice_id: str | None
    amount_cents: int
    status: str
    period_start: datetime | None
    period_end: datetime | None
    paid_at: datetime | None


@router.get("/clinics/{practice_id}/invoices", response_model=list[InvoiceRow])
async def admin_list_invoices(
    practice_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_permission(VIEW_BILLING_ALL)),
) -> list[InvoiceRow]:
    """A clinic's invoices (local mirror of Stripe's) — pick one to refund."""
    async with _app_db.async_session_factory() as session:
        rows = (await session.execute(
            select(Invoice).where(Invoice.practice_id == practice_id)
            .order_by(Invoice.created_at.desc()).limit(100)
        )).scalars().all()
    return [InvoiceRow(
        id=str(i.id), stripe_invoice_id=i.stripe_invoice_id,
        amount_cents=i.amount_cents, status=i.status,
        period_start=i.period_start, period_end=i.period_end, paid_at=i.paid_at,
    ) for i in rows]


class RefundRequest(BaseModel):
    # None → full refund of the invoice's payment.
    amount_cents: int | None = None


@router.post("/invoices/{invoice_id}/refund", response_model=InvoiceRow)
async def admin_refund_invoice(
    invoice_id: uuid.UUID,
    payload: RefundRequest,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_BILLING_ALL)),
) -> InvoiceRow:
    """Refund a paid invoice via Stripe (full, or partial with amount_cents).

    Irreversible money movement — the UI must confirm before calling. Concurrency-
    safe: the invoice row is locked (FOR UPDATE) so two simultaneous requests
    serialize, the refund is bounded by the REMAINING amount (partials accumulate
    in refunded_amount_cents), and the Stripe call carries a deterministic
    Idempotency-Key derived from the prior state — an accidental duplicate of the
    same refund collapses into one money movement on Stripe's side too."""
    if payload.amount_cents is not None and payload.amount_cents <= 0:
        raise HTTPException(status_code=422, detail="amount_cents must be > 0.")
    async with _app_db.async_session_factory() as session:
        inv = (await session.execute(
            select(Invoice).where(Invoice.id == invoice_id).with_for_update()
        )).scalar_one_or_none()
        if inv is None:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        if not inv.stripe_invoice_id:
            raise HTTPException(
                status_code=409,
                detail="Invoice has no Stripe payment (local/pilot record) — nothing to refund.",
            )
        if inv.status not in ("paid", "partially_refunded"):
            raise HTTPException(
                status_code=409, detail=f"Invoice is '{inv.status}' — only paid invoices refund.",
            )
        remaining = inv.amount_cents - inv.refunded_amount_cents
        if remaining <= 0:
            raise HTTPException(status_code=409, detail="Invoice is already fully refunded.")
        refund_amount = payload.amount_cents if payload.amount_cents is not None else remaining
        if refund_amount > remaining:
            raise HTTPException(
                status_code=422,
                detail=f"Refund exceeds the remaining amount ({remaining} cents).",
            )
        # Deterministic per prior state: a double-submit of the same refund carries
        # the SAME key (Stripe dedupes); after this refund lands, the next partial
        # sees a new refunded total → new key → correctly treated as intentional.
        idem = f"refund-{invoice_id}-{refund_amount}-{inv.refunded_amount_cents}"
        try:
            refund = await refund_invoice(
                inv.stripe_invoice_id, refund_amount, idempotency_key=idem
            )
        except (BillingNotConfigured, StripeError) as exc:
            raise _stripe_http(exc) from exc
        inv.refunded_amount_cents += refund_amount
        full = inv.refunded_amount_cents >= inv.amount_cents
        inv.status = "refunded" if full else "partially_refunded"
        await _audit(session, ctx, "admin_refund_invoice", practice_id=inv.practice_id,
                     meta={"invoice": str(invoice_id), "amount_cents": refund_amount,
                           "full": full, "refund_id": refund.get("id"),
                           "refunded_total_cents": inv.refunded_amount_cents})
        await session.commit()
        row = InvoiceRow(
            id=str(inv.id), stripe_invoice_id=inv.stripe_invoice_id,
            amount_cents=inv.amount_cents, status=inv.status,
            period_start=inv.period_start, period_end=inv.period_end, paid_at=inv.paid_at,
        )
    return row


# ===========================================================================
# 13. Pricing + business config (ADM6) — editable price grid + referral knobs.
# The public site reads GET /api/pricing; these endpoints let staff change prices
# and percents WITHOUT a deploy. Separate from billing/plans.py (Stripe metering).
# ===========================================================================
class PricingPlanRow(BaseModel):
    plan_key: str
    name: str
    monthly_cents: int
    annual_monthly_cents: int
    soft_cap_minutes: int
    overage_cents_per_min: int
    reactivation_level: str
    per_location: bool
    highlight: bool
    sort_order: int
    is_active: bool


class PricingPlanUpdate(BaseModel):
    # All optional → partial update (PATCH-like). Only provided fields change.
    name: str | None = None
    monthly_cents: int | None = None
    annual_monthly_cents: int | None = None
    soft_cap_minutes: int | None = None
    overage_cents_per_min: int | None = None
    reactivation_level: str | None = None
    per_location: bool | None = None
    highlight: bool | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class PricingConfigRow(BaseModel):
    referrer_reward_months: int
    invitee_discount_percent: int
    annual_discount_percent: int


class PricingConfigUpdate(BaseModel):
    referrer_reward_months: int | None = None
    invitee_discount_percent: int | None = None
    annual_discount_percent: int | None = None


def _plan_row(p: PricingPlan) -> PricingPlanRow:
    return PricingPlanRow(
        plan_key=p.plan_key, name=p.name, monthly_cents=p.monthly_cents,
        annual_monthly_cents=p.annual_monthly_cents, soft_cap_minutes=p.soft_cap_minutes,
        overage_cents_per_min=p.overage_cents_per_min,
        reactivation_level=p.reactivation_level, per_location=p.per_location,
        highlight=p.highlight, sort_order=p.sort_order, is_active=p.is_active,
    )


class AdminPricing(BaseModel):
    plans: list[PricingPlanRow]
    config: PricingConfigRow


@router.get("/pricing", response_model=AdminPricing)
async def admin_get_pricing(
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_PRICING)),
) -> AdminPricing:
    """Full editable grid (incl. inactive plans) + referral/annual config."""
    from app.services.pricing import get_all_plans, get_or_create_settings
    async with _app_db.async_session_factory() as session:
        plans = await get_all_plans(session)
        s = await get_or_create_settings(session)
        await session.commit()
        return AdminPricing(
            plans=[_plan_row(p) for p in plans],
            config=PricingConfigRow(
                referrer_reward_months=s.referrer_reward_months,
                invitee_discount_percent=s.invitee_discount_percent,
                annual_discount_percent=s.annual_discount_percent,
            ),
        )


_REACTIVATION_LEVELS = ("none", "basic", "full")


@router.put("/pricing/{plan_key}", response_model=PricingPlanRow)
async def admin_update_plan(
    plan_key: str,
    payload: PricingPlanUpdate,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_PRICING)),
) -> PricingPlanRow:
    """Edit one plan's price/limits. Money fields must be non-negative; percent-like
    fields are validated so a typo can't publish a broken grid to the public site."""
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update.")
    for money in ("monthly_cents", "annual_monthly_cents", "soft_cap_minutes",
                  "overage_cents_per_min", "sort_order"):
        if money in fields and fields[money] < 0:
            raise HTTPException(status_code=422, detail=f"{money} must be >= 0.")
    if "reactivation_level" in fields and fields["reactivation_level"] not in _REACTIVATION_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"reactivation_level must be one of {_REACTIVATION_LEVELS}.",
        )
    async with _app_db.async_session_factory() as session:
        plan = (await session.execute(
            select(PricingPlan).where(PricingPlan.plan_key == plan_key).with_for_update()
        )).scalar_one_or_none()
        if plan is None:
            raise HTTPException(status_code=404, detail=f"No plan '{plan_key}'.")
        before = _plan_row(plan).model_dump()
        for k, v in fields.items():
            setattr(plan, k, v)
        await _audit(session, ctx, "admin_update_pricing_plan",
                     meta={"plan_key": plan_key, "before": before, "changes": fields})
        await session.commit()
        return _plan_row(plan)


@router.put("/pricing-config", response_model=PricingConfigRow)
async def admin_update_pricing_config(
    payload: PricingConfigUpdate,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_PRICING)),
) -> PricingConfigRow:
    """Edit referral reward months + invitee/annual discount percents (singleton)."""
    from app.services.pricing import get_or_create_settings
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update.")
    # Bounded 0–24: a fat-fingered (or compromised-account) huge value would hand out
    # unlimited free-month credits — cap it well below anything a real promo needs.
    if "referrer_reward_months" in fields and not (0 <= fields["referrer_reward_months"] <= 24):
        raise HTTPException(status_code=422, detail="referrer_reward_months must be 0–24.")
    for pct in ("invitee_discount_percent", "annual_discount_percent"):
        if pct in fields and not (0 <= fields[pct] <= 100):
            raise HTTPException(status_code=422, detail=f"{pct} must be 0–100.")
    async with _app_db.async_session_factory() as session:
        s = await get_or_create_settings(session)
        before = {
            "referrer_reward_months": s.referrer_reward_months,
            "invitee_discount_percent": s.invitee_discount_percent,
            "annual_discount_percent": s.annual_discount_percent,
        }
        for k, v in fields.items():
            setattr(s, k, v)
        await _audit(session, ctx, "admin_update_pricing_config",
                     meta={"before": before, "changes": fields})
        await session.commit()
        return PricingConfigRow(
            referrer_reward_months=s.referrer_reward_months,
            invitee_discount_percent=s.invitee_discount_percent,
            annual_discount_percent=s.annual_discount_percent,
        )


# ===========================================================================
# 14. Subscription cancel / resume (ADM8) — REAL Stripe cancellation, not just
# the local status override above. Money-adjacent → confirm in the UI first.
# ===========================================================================
class CancelRequest(BaseModel):
    # 'at_period_end' (default): service runs until the paid period ends, then
    # billing stops. 'immediately': hard cancel right now (no auto-refund — use
    # the refund endpoint for that).
    mode: str = "at_period_end"


class CancelStateRow(BaseModel):
    practice_id: str
    status: str
    cancel_at_period_end: bool
    current_period_end: datetime | None


def _cancel_row(sub: Subscription) -> CancelStateRow:
    return CancelStateRow(
        practice_id=str(sub.practice_id), status=sub.status,
        cancel_at_period_end=sub.cancel_at_period_end,
        current_period_end=sub.current_period_end,
    )


@router.post("/clinics/{practice_id}/subscription/cancel", response_model=CancelStateRow)
async def admin_cancel_subscription(
    practice_id: uuid.UUID,
    payload: CancelRequest,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_SUBSCRIPTIONS)),
) -> CancelStateRow:
    """Cancel the clinic's Stripe subscription (default: at period end)."""
    if payload.mode not in ("at_period_end", "immediately"):
        raise HTTPException(status_code=422,
                            detail="mode must be 'at_period_end' or 'immediately'.")
    async with _app_db.async_session_factory() as session:
        sub = (await session.execute(
            select(Subscription).where(Subscription.practice_id == practice_id)
            .with_for_update()
        )).scalar_one_or_none()
        if sub is None:
            raise HTTPException(status_code=404, detail="Clinic has no subscription.")
        if not sub.stripe_subscription_id:
            raise HTTPException(
                status_code=409,
                detail="No Stripe subscription (pilot/manual deal) — set status via "
                       "the subscription override instead.",
            )
        if sub.status == "cancelled":
            raise HTTPException(status_code=409, detail="Already cancelled.")
        if payload.mode == "at_period_end" and sub.cancel_at_period_end:
            raise HTTPException(status_code=409,
                                detail="Cancellation is already scheduled.")
        try:
            await cancel_subscription(
                sub.stripe_subscription_id,
                at_period_end=(payload.mode == "at_period_end"),
            )
        except (BillingNotConfigured, StripeError) as exc:
            raise _stripe_http(exc) from exc
        if payload.mode == "immediately":
            sub.status = "cancelled"
            sub.cancel_at_period_end = False
            p = (await session.execute(
                select(Practice).where(Practice.id == practice_id)
            )).scalar_one_or_none()
            if p is not None:
                p.status = "suspended"
        else:
            sub.cancel_at_period_end = True
        await _audit(session, ctx, "admin_cancel_subscription",
                     practice_id=practice_id, meta={"mode": payload.mode})
        await session.commit()
        row = _cancel_row(sub)
    return row


@router.post("/clinics/{practice_id}/subscription/resume", response_model=CancelStateRow)
async def admin_resume_subscription(
    practice_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_SUBSCRIPTIONS)),
) -> CancelStateRow:
    """Undo a scheduled at-period-end cancellation (before the period ends)."""
    async with _app_db.async_session_factory() as session:
        sub = (await session.execute(
            select(Subscription).where(Subscription.practice_id == practice_id)
            .with_for_update()
        )).scalar_one_or_none()
        if sub is None:
            raise HTTPException(status_code=404, detail="Clinic has no subscription.")
        if not sub.stripe_subscription_id:
            raise HTTPException(status_code=409, detail="No Stripe subscription.")
        if sub.status == "cancelled":
            raise HTTPException(
                status_code=409,
                detail="Subscription is fully cancelled — create a new checkout instead.",
            )
        if not sub.cancel_at_period_end:
            raise HTTPException(status_code=409, detail="No pending cancellation.")
        try:
            await resume_subscription(sub.stripe_subscription_id)
        except (BillingNotConfigured, StripeError) as exc:
            raise _stripe_http(exc) from exc
        sub.cancel_at_period_end = False
        await _audit(session, ctx, "admin_resume_subscription", practice_id=practice_id)
        await session.commit()
        row = _cancel_row(sub)
    return row


# ===========================================================================
# 15. Staff invite / deactivate (ADM9) — manage internal admins from the panel.
# Invite: Clerk instance-invitation carrying dentiva_role in public_metadata; on
# signup the user.created webhook provisions is_internal + dentiva_staff (see
# clerk_provisioning._invited_dentiva_role). No manual script step.
# ===========================================================================
from app.auth.permissions import ADMIN_ROLE_PERMISSIONS  # noqa: E402 — section import
from app.services.clerk_api import (  # noqa: E402 — grouped with its section
    ClerkError,
    create_platform_invitation,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class StaffInvite(BaseModel):
    email: str
    role: str  # super_admin | support | sales | finance | engineer


@router.post("/staff/invite")
async def admin_invite_staff(
    payload: StaffInvite,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_DENTIVA_STAFF)),
) -> dict:
    """Email an internal-staff invitation. The invitee signs up via Clerk's link
    and lands provisioned with the given Dentiva role — nothing manual."""
    email = payload.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Invalid email.")
    if payload.role not in ADMIN_ROLE_PERMISSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"role must be one of {sorted(ADMIN_ROLE_PERMISSIONS)}.",
        )
    async with _app_db.async_session_factory() as session:
        existing = (await session.execute(
            select(User).where(func.lower(User.email) == email)
        )).scalar_one_or_none()
        if existing is not None and existing.is_internal:
            raise HTTPException(status_code=409, detail="Already internal staff.")
        try:
            invitation_id = await create_platform_invitation(
                email=email, public_metadata={"dentiva_role": payload.role},
            )
        except ClerkError as exc:
            status_ = exc.status_code or 422
            raise HTTPException(
                status_code=502 if status_ >= 500 else (503 if status_ == 503 else 422),
                detail=str(exc),
            ) from exc
        await _audit(session, ctx, "admin_invite_staff",
                     meta={"email": email, "role": payload.role,
                           "invitation": invitation_id})
        await session.commit()
    return {"ok": True, "invitation_id": invitation_id}


@router.delete("/staff/{user_id}")
async def admin_deactivate_staff(
    user_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_DENTIVA_STAFF)),
) -> dict:
    """Revoke a person's admin access: removes their dentiva_staff role row —
    every /api/admin/* request 403s immediately. The Clerk account itself stays
    (they may still be a clinic user); safe and reversible via a fresh invite."""
    if user_id == ctx.user.id:
        raise HTTPException(status_code=409, detail="You cannot deactivate yourself.")
    async with _app_db.async_session_factory() as session:
        staff = (await session.execute(
            select(DentivaStaff).where(DentivaStaff.user_id == user_id)
        )).scalar_one_or_none()
        if staff is None:
            raise HTTPException(status_code=404, detail="No staff role for that user.")
        if staff.role == "super_admin":
            # Lockout guard (reviewer): only super_admin holds MANAGE_DENTIVA_STAFF,
            # so removing the LAST one would freeze staff management forever (two
            # super_admins nuking each other included). Lock the super_admin rows
            # to serialize concurrent deactivations, then count.
            sa_rows = (await session.execute(
                select(DentivaStaff).where(DentivaStaff.role == "super_admin")
                .with_for_update()
            )).scalars().all()
            if len(sa_rows) <= 1:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot deactivate the last super_admin — promote "
                           "someone else first.",
                )
        removed_role = staff.role
        await session.delete(staff)
        await _audit(session, ctx, "admin_deactivate_staff",
                     meta={"user_id": str(user_id), "removed_role": removed_role})
        await session.commit()
    return {"ok": True, "removed_role": removed_role}
