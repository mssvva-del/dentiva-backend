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
    MANAGE_DENTIVA_STAFF,
    MANAGE_FEATURE_FLAGS,
    MANAGE_LEADS,
    MANAGE_SUBSCRIPTIONS,
    VIEW_ALL_CLINICS,
    VIEW_AUDIT_LOGS,
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
