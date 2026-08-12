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

import logging
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import app.db as _app_db
from app.auth.admin import AdminContext, require_admin_permission, require_internal
from app.auth.permissions import (
    IMPERSONATE_CLINIC,
    MANAGE_BILLING_ALL,
    MANAGE_CLINIC_STATUS,
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
from app.billing.metering import _month_bounds
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

logger = logging.getLogger("dentiva.admin")


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
    # Staff still see the canary — its calls are how you debug the monitor — but
    # they see that it is one. A monitoring tenant counted as a customer is a
    # number somebody eventually reports to someone else.
    is_canary: bool = False
    # Minutes THIS period against what they pay for — the column that turns a
    # list of names into a list of clinics worth looking at. Without it the only
    # way to find the practice eating three times its bucket is to open all of
    # them, which nobody does twice.
    period_minutes_used: float = 0.0
    period_minutes_included: int | None = None


@router.get("/clinics", response_model=list[ClinicRow])
async def list_clinics(
    ctx: AdminContext = Depends(require_admin_permission(VIEW_ALL_CLINICS)),
) -> list[ClinicRow]:
    async with _app_db.platform_session_factory() as session:
        rows = (
            await session.execute(select(Practice).order_by(Practice.created_at.desc()))
        ).scalars().all()
        subs = {
            s.practice_id: s
            for s in (await session.execute(select(Subscription))).scalars().all()
        }
        # One grouped query rather than one per clinic. At seven practices the
        # difference is invisible and at seventy it is the page.
        month_start, _ = _month_bounds(datetime.now(tz=UTC))
        usage = dict((await session.execute(
            select(UsageRecord.practice_id, func.coalesce(func.sum(UsageRecord.minutes_used), 0))
            .where(UsageRecord.period_start >= month_start)
            .group_by(UsageRecord.practice_id)
        )).all())
    return [
        ClinicRow(
            id=str(p.id), name=p.name, status=p.status,
            plan=subs[p.id].plan if p.id in subs else None,
            mrr_cents=subs[p.id].mrr_cents if p.id in subs else 0,
            onboarding_step=p.onboarding_step, created_at=p.created_at,
            is_canary=p.is_canary,
            period_minutes_used=float(usage.get(p.id, 0)),
            period_minutes_included=subs[p.id].included_minutes if p.id in subs else None,
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
    # Pending-cancel state (ADM8) — lets the clinic card show a "Cancels on {date}"
    # banner + Resume without a second round-trip.
    cancel_at_period_end: bool
    current_period_end: datetime | None
    user_count: int
    call_count: int
    booking_count: int
    # THIS period, against what they pay for. Lifetime counts say a clinic has
    # been busy; they cannot say whether it is about to cost us money. A practice
    # quietly running at three times its bucket looks identical to a happy one
    # until the invoice.
    period_minutes_used: float = 0.0
    period_minutes_included: int | None = None
    # ADM-CLIENT-360: the full clinic profile in one card.
    address: str | None = None
    phone_number: str | None = None
    transfer_phone_number: str | None = None
    ai_phone_number: str | None = None
    forwarding_instruction: str = ""
    business_hours: dict = {}
    agent_name: str | None = None
    agent_greeting: str | None = None
    onboarding_step: int = 0
    created_at: datetime | None = None
    owner_email: str | None = None
    # Knowledge-base coverage (counts only — no PHI): shows how "trained" the agent is.
    kb_providers: int = 0
    kb_insurances: int = 0
    kb_has_policies: bool = False
    kb_has_emergency: bool = False


class BaaHistoryRow(BaseModel):
    document_version: str
    signer_name: str
    signer_title: str
    signed_at: datetime | None
    signer_ip: str | None


class ClinicEdit(BaseModel):
    """Admin-side edits to a clinic profile (super-admin support ops)."""
    name: str | None = None
    timezone: str | None = None
    phone_number: str | None = None
    transfer_phone_number: str | None = None
    business_hours: dict | None = None
    languages_enabled: list[str] | None = None
    agent_name: str | None = None
    agent_greeting: str | None = None


class CreateClinicRequest(BaseModel):
    name: str
    timezone: str = "America/New_York"
    owner_email: str | None = None


@router.post("/clinics", response_model=ClinicRow, status_code=201)
async def create_clinic(
    body: CreateClinicRequest,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_CLINIC_STATUS)),
) -> ClinicRow:
    """Stand up a clinic from the admin panel.

    There was no way to do this, and the reason is worth stating rather than
    working around: identity lives in Clerk. Both paths that create a practice
    key on a Clerk organization, so a clinic created here without one is an
    orphan — nobody can sign in to it, and when the owner eventually creates
    their own organization a SECOND practice appears beside it, with the calls
    and the billing on the wrong one.

    So the organization is created FIRST, and if that fails nothing else happens.
    A half-created clinic is worse than no button: the row looks real in every
    list while being unreachable, and the person who finds out is the customer
    trying to log in.

    The clinic lands in onboarding at step one, exactly where it would be if the
    owner had signed up themselves — the panel skips the sign-up, not the setup.
    """
    from app.services.clerk_api import create_org_invitation, create_organization

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="A clinic needs a name.")

    clerk_org_id = await create_organization(name=name)
    if not clerk_org_id:
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not create the organization in Clerk, so no clinic was "
                "created. A clinic without one cannot be signed in to."
            ),
        )

    async with _app_db.platform_session_factory() as session:
        practice = Practice(
            id=uuid.uuid4(),
            clerk_org_id=clerk_org_id,
            name=name,
            timezone=body.timezone,
            pms_system="none",
            business_hours={d: None for d in
                            ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
            languages_enabled=["en", "es"],
            status="onboarding",
            onboarding_step=1,
        )
        session.add(practice)
        await session.flush()
        await _audit(
            session, ctx, "admin_create_clinic", practice_id=practice.id,
            meta={"name": name, "invited": bool(body.owner_email)},
        )
        await session.commit()
        row = ClinicRow(
            id=str(practice.id), name=practice.name, status=practice.status,
            plan=None, mrr_cents=0, onboarding_step=practice.onboarding_step,
            created_at=practice.created_at, is_canary=False,
            period_minutes_used=0.0, period_minutes_included=None,
        )

    if body.owner_email:
        # Best effort, and deliberately after the commit: the clinic exists
        # either way, and an invitation can be resent. Failing the whole request
        # over a bounced email would leave the operator retrying a create that
        # would then make a second organization.
        await create_org_invitation(
            clerk_org_id=clerk_org_id, email=body.owner_email.strip(),
            clerk_role="org:admin",
        )
    return row


@router.get("/clinics/{practice_id}", response_model=ClinicDetail)
async def clinic_detail(
    practice_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_permission(VIEW_CLINIC_DETAIL)),
) -> ClinicDetail:
    async with _app_db.platform_session_factory() as session:
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
        owner_email = (await session.execute(
            select(User.email).where(User.practice_id == practice_id, User.role == "owner")
            .limit(1)
        )).scalar_one_or_none()
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
        # Minutes THIS period, against what they pay for. The subscription's own
        # window when it has one, otherwise the calendar month — the same rule
        # metering writes by, so the two can never disagree about which period
        # a call belongs to.
        if sub and sub.current_period_start:
            usage_from = sub.current_period_start
        else:
            usage_from, _ = _month_bounds(datetime.now(tz=UTC))
        period_minutes = float((await session.execute(
            select(func.coalesce(func.sum(UsageRecord.minutes_used), 0))
            .where(
                UsageRecord.practice_id == practice_id,
                UsageRecord.period_start >= usage_from,
            )
        )).scalar_one())
        await _audit(session, ctx, "admin_view_clinic", practice_id=practice_id)
        await session.commit()

    from app.config import get_settings as _gs
    from app.services.call_routing import forwarding_instruction
    _s = _gs()
    agent = p.agent_settings or {}
    kb = p.knowledge_base or {}
    return ClinicDetail(
        id=str(p.id), name=p.name, status=p.status, timezone=p.timezone,
        pms_system=p.pms_system, languages_enabled=list(p.languages_enabled),
        plan=sub.plan if sub else None,
        subscription_status=sub.status if sub else None,
        included_minutes=sub.included_minutes if sub else None,
        mrr_cents=sub.mrr_cents if sub else 0,
        cancel_at_period_end=bool(sub.cancel_at_period_end) if sub else False,
        current_period_end=sub.current_period_end if sub else None,
        user_count=users, call_count=calls, booking_count=bookings,
        period_minutes_used=period_minutes,
        period_minutes_included=sub.included_minutes if sub else None,
        address=p.address, phone_number=p.phone_number,
        transfer_phone_number=p.transfer_phone_number,
        # This clinic's own provisioned number (NUM-1); global env is the
        # pre-NUM-1 fallback so older practices still display something.
        ai_phone_number=p.ai_phone_number or _s.retell_from_number or None,
        forwarding_instruction=forwarding_instruction(
            answer_mode=p.answer_mode, rings_before_ai=p.rings_before_ai,
            ai_number=p.ai_phone_number or _s.retell_from_number or None),
        business_hours=p.business_hours or {},
        agent_name=(str(agent.get("agent_name") or "").strip() or "Alex"),
        agent_greeting=(str(agent.get("greeting") or "").strip() or None),
        onboarding_step=p.onboarding_step, created_at=p.created_at,
        owner_email=owner_email,
        kb_providers=len(kb.get("providers") or []),
        kb_insurances=len(kb.get("insurances") or []),
        kb_has_policies=bool(any((kb.get("policies") or {}).values())),
        kb_has_emergency=bool(kb.get("emergency")),
    )


@router.get("/clinics/{practice_id}/baa-history", response_model=list[BaaHistoryRow])
async def clinic_baa_history(
    practice_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_permission(VIEW_CLINIC_DETAIL)),
) -> list[BaaHistoryRow]:
    """Signed-BAA history for a clinic — the compliance record (who signed which
    version, when, from what IP). Read-only, audited."""
    from app.models.baa_acceptance import BaaAcceptance
    async with _app_db.platform_session_factory() as session:
        rows = (await session.execute(
            select(BaaAcceptance).where(BaaAcceptance.practice_id == practice_id)
            .order_by(BaaAcceptance.created_at.desc())
        )).scalars().all()
        await _audit(session, ctx, "admin_view_baa_history", practice_id=practice_id)
        await session.commit()
    return [
        BaaHistoryRow(
            document_version=r.document_version, signer_name=r.signer_name,
            signer_title=r.signer_title, signed_at=r.created_at, signer_ip=r.signer_ip,
        ) for r in rows
    ]


@router.patch("/clinics/{practice_id}", response_model=ClinicDetail)
async def edit_clinic(
    practice_id: uuid.UUID,
    payload: ClinicEdit,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_CLINIC_STATUS)),
) -> ClinicDetail:
    """Admin edit of a clinic profile (support ops). Only provided fields change;
    agent persona is merged into agent_settings. Audited with the changed keys."""
    async with _app_db.platform_session_factory() as session:
        p = (await session.execute(
            select(Practice).where(Practice.id == practice_id)
        )).scalar_one_or_none()
        if p is None:
            raise HTTPException(status_code=404, detail="Clinic not found.")
        changed: list[str] = []
        for field in ("name", "timezone", "phone_number", "transfer_phone_number",
                      "business_hours", "languages_enabled"):
            val = getattr(payload, field)
            if val is not None and val != getattr(p, field):
                setattr(p, field, val)
                changed.append(field)
        if payload.agent_name is not None or payload.agent_greeting is not None:
            agent = dict(p.agent_settings or {})
            if payload.agent_name is not None and payload.agent_name.strip():
                agent["agent_name"] = payload.agent_name.strip()
                changed.append("agent_name")
            if payload.agent_greeting is not None:
                agent["greeting"] = payload.agent_greeting.strip()
                changed.append("agent_greeting")
            p.agent_settings = agent
        await _audit(session, ctx, "admin_edit_clinic", practice_id=practice_id,
                     meta={"changed": changed})
        await session.commit()
    # Return the fresh detail (reuse the reader).
    return await clinic_detail(practice_id, ctx)  # type: ignore[arg-type]


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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
            plan_key = payload.plan or "after_hours"
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
    async with _app_db.platform_session_factory() as session:
        subs = (await session.execute(select(Subscription))).scalars().all()
        # The canary is a monitoring tenant, not a customer. Counting it inflates
        # every clinic figure on this page by one, and this page is where those
        # figures get quoted to other people.
        practices = (await session.execute(
            select(Practice).where(Practice.is_canary.is_(False))
        )).scalars().all()
        # THIS month, not all of history. This summed every usage row ever
        # written while calling itself period_minutes, so the cost and margin
        # below drifted further from the truth with every call the product had
        # ever taken — a number that can only grow, presented as a monthly one.
        period_start, _ = _month_bounds(datetime.now(tz=UTC))
        minutes = (await session.execute(
            select(func.coalesce(func.sum(UsageRecord.minutes_used), 0))
            .where(UsageRecord.period_start >= period_start)
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    # Is the tenant boundary actually on, and if not, can it be switched on safely?
    rls_enforced: bool | None = None
    rls_switch_blockers: list[str] = []


class AdminMe(BaseModel):
    role: str
    permissions: list[str]


@router.get("/me", response_model=AdminMe)
async def admin_me(ctx: AdminContext = Depends(require_internal())) -> AdminMe:
    """Who the internal caller is and what they may do.

    The server is and stays the boundary — every admin route re-checks its own
    permission. This exists so the UI can stop advertising doors that will not
    open: a support user was shown Revenue, Pricing and Feature flags and got a
    generic error on each, which reads like a broken product rather than a role.
    """
    from app.auth.permissions import admin_permissions_for

    return AdminMe(
        role=ctx.staff_role,
        permissions=sorted(admin_permissions_for(ctx.staff_role)),
    )


@router.get("/system-health", response_model=SystemHealth)
async def system_health(
    ctx: AdminContext = Depends(require_admin_permission(VIEW_SYSTEM_HEALTH)),
) -> SystemHealth:
    from app.config import get_settings
    db_ok = True
    clinics = staff = 0
    rls_enforced: bool | None = None
    blockers: list[str] = []
    try:
        async with _app_db.platform_session_factory() as session:
            clinics = (await session.execute(
                select(func.count()).select_from(Practice)
                .where(Practice.is_canary.is_(False))
            )).scalar_one()
            staff = (await session.execute(
                select(func.count()).select_from(DentivaStaff)
            )).scalar_one()
            try:
                rls_enforced, blockers = await _rls_switch_report(session)
            except Exception:  # noqa: BLE001
                # A diagnostic must never be able to report the database as down.
                # This one did exactly that: it threw, the outer handler set
                # db_ok=False, and the page announced "Database Unreachable" while
                # the database was serving calls perfectly well.
                logger.exception("RLS switch pre-flight failed")
                blockers = ["pre-flight check itself failed — see logs"]
    except Exception:  # noqa: BLE001
        db_ok = False
    return SystemHealth(
        db_ok=db_ok, clinics=clinics, internal_staff=staff,
        environment=get_settings().environment,
        rls_enforced=rls_enforced, rls_switch_blockers=blockers,
    )


async def _rls_switch_report(session) -> tuple[bool | None, list[str]]:
    """Is RLS on, and would repointing DATABASE_URL at dentiva_app actually work?

    Answering this BEFORE the switch matters: the alternative is finding out from
    a live call failing with "permission denied for table …". Read-only.
    """
    row = (await session.execute(text(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ))).first()
    enforced = None if row is None else not (bool(row[0]) or bool(row[1]))
    if enforced:
        return True, []  # already switched; nothing to pre-flight

    blockers: list[str] = []
    role = (await session.execute(text(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'dentiva_app'"
    ))).first()
    if role is None:
        blockers.append(
            "role dentiva_app does not exist — run migrations (b2c3d4e5f6a7)"
        )
        return enforced, blockers
    if bool(role[0]) or bool(role[1]):
        blockers.append(
            "role dentiva_app is SUPERUSER/BYPASSRLS — switching to it would change "
            "nothing; it must be NOSUPERUSER NOBYPASSRLS"
        )

    # Any table the app would suddenly be unable to read is a live outage.
    # Join through pg_class and test the OID, not the name. Passing a name makes
    # Postgres resolve it through search_path, and the planner is free to evaluate
    # that before the schema filter — which it does, blowing up on the first
    # information_schema table it meets ("relation sql_features does not exist").
    missing = (await session.execute(text(
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND NOT has_table_privilege('dentiva_app', c.oid, 'SELECT')
        ORDER BY c.relname
        """
    ))).scalars().all()
    if missing:
        blockers.append(
            "dentiva_app cannot read: " + ", ".join(missing[:8])
            + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else "")
            + " — run migration n5c6d7e8f9a0"
        )
    return enforced, blockers


# ===========================================================================
# 9b. Self-learning loop — QA review of failed calls (QA-LOOP-1)
# ===========================================================================
class QaFinding(BaseModel):
    call_id: str
    outcome: str | None
    lost_caller: bool
    break_point: str
    why: str
    prompt_fix: str
    category: str


class QaPattern(BaseModel):
    category: str
    count: int
    actionable: bool  # seen 3+ times → worth a prompt change (B&H rule)
    fixes: list[str]


class QaBrokenPromise(BaseModel):
    call_id: str
    outcome: str | None
    reasons: list[str]


class QaReview(BaseModel):
    reviewed: int
    lost_callers: int
    # Non-empty = the agent promised something we don't deliver. Release blocker,
    # not a nice-to-have; scanned on EVERY recent call, not just failed ones.
    broken_promises: list[QaBrokenPromise] = []
    patterns: list[QaPattern]
    findings: list[QaFinding]


@router.get("/qa/call-review", response_model=QaReview)
async def qa_call_review(
    limit: int = 15,
    ctx: AdminContext = Depends(require_admin_permission(VIEW_SYSTEM_HEALTH)),
) -> QaReview:
    """Analyze recent FAILED calls → surface repeating failure patterns + prompt
    fixes. Read-only; never auto-edits the prompt (a human applies). Cross-clinic
    coordinator view of the shared agent's weak spots.
    """
    from app.services.qa.call_review import review_recent_failures
    limit = max(1, min(limit, 50))
    async with _app_db.platform_session_factory() as session:
        result = await review_recent_failures(session, limit=limit)
    return QaReview(**result)


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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    async with _app_db.platform_session_factory() as session:
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
    find_clerk_user_by_email,
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
    async with _app_db.platform_session_factory() as session:
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
            # Clerk refuses invitations for addresses that ALREADY have an account
            # ("That email address is taken"). That's not a dead end — the person
            # exists, so PROMOTE them directly: find their Clerk id, upsert our
            # user row as internal, attach the staff role. Same net effect as an
            # accepted invitation, minus the email round-trip.
            if exc.status_code == 422 and "taken" in str(exc).lower():
                promoted = await _promote_existing_to_staff(
                    session, ctx, email=email, role=payload.role, local=existing,
                )
                if promoted:
                    return {"ok": True, "invitation_id": "", "promoted": True}
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


async def _promote_existing_to_staff(
    session: AsyncSession, ctx: AdminContext, *, email: str, role: str,
    local: User | None,
) -> bool:
    """Grant an EXISTING Clerk account the internal staff role (invite fallback).

    Returns False when no Clerk user actually matches (caller re-raises the
    original Clerk error). Upserts our users row (is_internal=True) keyed on the
    Clerk id, then attaches the DentivaStaff role. Audited as a promotion —
    distinct from an emailed invitation."""
    clerk_id = await find_clerk_user_by_email(email)
    if clerk_id is None:
        return False
    user = local
    if user is None:
        user = (await session.execute(
            select(User).where(User.clerk_user_id == clerk_id)
        )).scalar_one_or_none()
    if user is None:
        user = User(
            id=uuid.uuid4(), clerk_user_id=clerk_id, practice_id=None,
            email=email, role="staff", is_internal=True, status="active",
        )
        session.add(user)
        await session.flush()
    else:
        user.is_internal = True
    staff = (await session.execute(
        select(DentivaStaff).where(DentivaStaff.user_id == user.id)
    )).scalar_one_or_none()
    if staff is None:
        session.add(DentivaStaff(id=uuid.uuid4(), user_id=user.id, role=role))
    else:
        staff.role = role
    await _audit(session, ctx, "admin_promote_staff",
                 meta={"email": email, "role": role, "clerk_user": clerk_id[:16]})
    await session.commit()
    return True


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
    async with _app_db.platform_session_factory() as session:
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


# ===========================================================================
# 16. Clinic notes (ADM10) — internal CRM commentary on an account.
# Read = VIEW_CLINIC_DETAIL (anyone who can open the clinic). Write = same (any
# viewing staffer may leave a note). Delete = the author OR a super_admin.
# ===========================================================================
from app.models.clinic_note import ClinicNote  # noqa: E402 — grouped with section


class ClinicNoteRow(BaseModel):
    id: str
    body: str
    author_email: str | None
    created_at: datetime


class ClinicNoteCreate(BaseModel):
    body: str


def _note_row(n: ClinicNote) -> ClinicNoteRow:
    return ClinicNoteRow(id=str(n.id), body=n.body, author_email=n.author_email,
                         created_at=n.created_at)


@router.get("/clinics/{practice_id}/notes", response_model=list[ClinicNoteRow])
async def list_clinic_notes(
    practice_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_permission(VIEW_CLINIC_DETAIL)),
) -> list[ClinicNoteRow]:
    """Account notes, newest first."""
    async with _app_db.platform_session_factory() as session:
        rows = (await session.execute(
            select(ClinicNote).where(ClinicNote.practice_id == practice_id)
            .order_by(ClinicNote.created_at.desc()).limit(200)
        )).scalars().all()
    return [_note_row(n) for n in rows]


@router.post("/clinics/{practice_id}/notes", response_model=ClinicNoteRow)
async def add_clinic_note(
    practice_id: uuid.UUID,
    payload: ClinicNoteCreate,
    ctx: AdminContext = Depends(require_admin_permission(VIEW_CLINIC_DETAIL)),
) -> ClinicNoteRow:
    body = payload.body.strip()
    if not body:
        raise HTTPException(status_code=422, detail="Note body is empty.")
    if len(body) > 5000:
        raise HTTPException(status_code=422, detail="Note too long (max 5000).")
    async with _app_db.platform_session_factory() as session:
        exists = (await session.execute(
            select(func.count()).select_from(Practice).where(Practice.id == practice_id)
        )).scalar_one()
        if not exists:
            raise HTTPException(status_code=404, detail="Clinic not found.")
        note = ClinicNote(
            id=uuid.uuid4(), practice_id=practice_id,
            author_user_id=ctx.user.id, author_email=ctx.user.email, body=body,
        )
        session.add(note)
        await _audit(session, ctx, "admin_add_clinic_note", practice_id=practice_id,
                     meta={"note_id": str(note.id)})
        await session.commit()
        await session.refresh(note)
        row = _note_row(note)
    return row


@router.delete("/clinics/{practice_id}/notes/{note_id}")
async def delete_clinic_note(
    practice_id: uuid.UUID,
    note_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_permission(VIEW_CLINIC_DETAIL)),
) -> dict:
    """Delete a note. Allowed for the note's AUTHOR or a super_admin — a viewer
    can't erase someone else's account history. Scoped under the clinic path
    (reviewer): the note must belong to {practice_id}, mirroring GET/POST so a
    note id can't be operated on outside its clinic context."""
    async with _app_db.platform_session_factory() as session:
        note = (await session.execute(
            select(ClinicNote).where(
                ClinicNote.id == note_id, ClinicNote.practice_id == practice_id
            )
        )).scalar_one_or_none()
        if note is None:
            raise HTTPException(status_code=404, detail="Note not found.")
        if note.author_user_id != ctx.user.id and ctx.staff_role != "super_admin":
            raise HTTPException(
                status_code=403,
                detail="Only the note's author or a super_admin can delete it.",
            )
        await session.delete(note)
        await _audit(session, ctx, "admin_delete_clinic_note", practice_id=practice_id,
                     meta={"note_id": str(note_id)})
        await session.commit()
    return {"ok": True}


# ===========================================================================
# 17. Voice model switch — change the LLM behind the live Retell agent from the
# panel (Sergio: "самую сильную, но чтобы в админке переключать"). Gated by
# MANAGE_FEATURE_FLAGS (super_admin + engineer); every switch audited + agent
# republished so the change is live immediately.
# ===========================================================================
from app.services.retell_admin import (  # noqa: E402 — grouped with its section
    ALLOWED_VOICE_MODELS,
    RetellError,
    RetellNotConfigured,
    get_agent,
    get_agent_versions,
    get_llm,
    list_phone_numbers,
    publish_agent,
    set_llm_model,
)


class VoiceModelState(BaseModel):
    model: str | None
    allowed: list[str]
    agent_id: str


class VoiceModelUpdate(BaseModel):
    model: str


async def _live_agent_id() -> str:
    """The live agent: env pin first, else the practice binding (single source
    the webhooks already use)."""
    from app.config import get_settings
    env_id = get_settings().retell_agent_id
    if env_id:
        return env_id
    async with _app_db.platform_session_factory() as session:
        agent_id = (await session.execute(
            select(Practice.retell_agent_id)
            .where(Practice.retell_agent_id.isnot(None)).limit(1)
        )).scalar_one_or_none()
    if not agent_id:
        raise HTTPException(status_code=404,
                            detail="No live agent configured (retell_agent_id).")
    return agent_id


def _retell_http(exc: Exception) -> HTTPException:
    if isinstance(exc, RetellNotConfigured):
        return HTTPException(status_code=503, detail="Retell is not configured.")
    status_ = getattr(exc, "status_code", None) or 422
    return HTTPException(status_code=502 if status_ >= 500 else 422, detail=str(exc))


@router.get("/voice/model", response_model=VoiceModelState)
async def get_voice_model(
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_FEATURE_FLAGS)),
) -> VoiceModelState:
    agent_id = await _live_agent_id()
    try:
        agent = await get_agent(agent_id)
        llm_id = (agent.get("response_engine") or {}).get("llm_id")
        model = None
        if llm_id:
            model = (await get_llm(llm_id)).get("model")
    except (RetellNotConfigured, RetellError) as exc:
        raise _retell_http(exc) from exc
    return VoiceModelState(model=model, allowed=list(ALLOWED_VOICE_MODELS),
                           agent_id=agent_id)


class VoiceLiveConfig(BaseModel):
    agent_id: str
    # What a patient dialling the clinic actually reaches. A Retell number pins an
    # agent VERSION and publishing does not move that pin, so these two can differ
    # by months — ours did, silently.
    published_version: int | None = None
    phone_numbers: list[str] = []
    voice_id: str | None
    voice_model: str | None
    interruption_sensitivity: float | None
    end_call_after_silence_ms: int | None
    model: str | None
    tools: list[str]
    native_transfer_tools: list[str]
    has_emergency_room_branch: bool
    promises_a_bridge_it_cannot_make: bool
    drift: list[str]


@router.get("/voice/live-config", response_model=VoiceLiveConfig)
async def get_voice_live_config(
    ctx: AdminContext = Depends(require_admin_permission(VIEW_SYSTEM_HEALTH)),
) -> VoiceLiveConfig:
    """What the agent CALLERS actually reach is configured to do, read live.

    Repo files and the live agent have already drifted twice — a prompt fix was
    published to one agent while production answered on another, and the voice
    tuning in the repo never matched the live one. This reads the real thing
    through the server's own Retell key and names the differences that change
    what a caller hears.
    """
    agent_id = await _live_agent_id()
    try:
        agent = await get_agent(agent_id)
        llm_id = (agent.get("response_engine") or {}).get("llm_id")
        llm = await get_llm(llm_id) if llm_id else {}
    except (RetellNotConfigured, RetellError) as exc:
        raise _retell_http(exc) from exc

    # What the NUMBER serves — the only view that matches what a caller hears.
    published_version: int | None = None
    stale_numbers: list[str] = []
    numbers: list[str] = []
    try:
        versions = await get_agent_versions(agent_id)
        published_version = max(
            (v["version"] for v in versions if v.get("is_published")), default=None
        )
        for number in await list_phone_numbers():
            bound = [
                a for a in (number.get("inbound_agents") or [])
                if a.get("agent_id") == agent_id
            ]
            if not bound:
                continue
            numbers.append(number["phone_number"])
            if published_version is not None and any(
                a.get("agent_version") != published_version for a in bound
            ):
                served = ", ".join(str(a.get("agent_version")) for a in bound)
                stale_numbers.append(f"{number['phone_number']} (v{served})")
    except (RetellNotConfigured, RetellError):
        pass  # the checks below still work; this one is extra

    prompt = llm.get("general_prompt") or ""
    tools = llm.get("general_tools") or []
    native = [t.get("name", "") for t in tools if t.get("type") == "transfer_call"]
    # The exact wording that left callers listening to silence — but promising a
    # connection is only a lie when the agent has no way to make one. With a
    # native transfer tool present the phrase is legitimate, and flagging it then
    # trains everyone to ignore this list.
    promises_bridge = bool(
        re.search(r"connect(ing)? you|transferr?ing you|put you through",
                  prompt, re.IGNORECASE)
    )
    lying = promises_bridge and not native
    has_er = "911" in prompt or "emergency room" in prompt.lower()

    drift: list[str] = []
    if stale_numbers:
        drift.append(
            "a phone number is answering with an OLD agent version — publishing "
            "does not move the number's pin: "
            + "; ".join(stale_numbers)
            + f" while v{published_version} is published"
        )
    if lying:
        drift.append(
            "prompt still promises to connect the caller — a custom tool cannot "
            "bridge a call, so that promise ends in silence"
        )
    if not has_er:
        drift.append(
            "no emergency-room branch: a caller who can't breathe would be put in "
            "the callback queue"
        )
    if not native:
        drift.append("no native transfer_call tool — escalation can only be a callback")
    sens = agent.get("interruption_sensitivity")
    if sens is not None and sens > 0.7:
        drift.append(
            f"interruption_sensitivity={sens}: above 0.7 the agent chops its own "
            "speech on echo"
        )
    if "{{office_status}}" not in prompt and "{{callback_eta}}" not in prompt:
        drift.append(
            "callback promise is not hours-aware — at 11pm it still says the team "
            "will call back shortly"
        )

    return VoiceLiveConfig(
        agent_id=agent_id,
        published_version=published_version,
        phone_numbers=numbers,
        voice_id=agent.get("voice_id"),
        voice_model=agent.get("voice_model"),
        interruption_sensitivity=sens,
        end_call_after_silence_ms=agent.get("end_call_after_silence_ms"),
        model=llm.get("model"),
        tools=[t.get("name", "") for t in tools],
        native_transfer_tools=native,
        has_emergency_room_branch=has_er,
        promises_a_bridge_it_cannot_make=lying,
        drift=drift,
    )


@router.put("/voice/model", response_model=VoiceModelState)
async def set_voice_model(
    payload: VoiceModelUpdate,
    ctx: AdminContext = Depends(require_admin_permission(MANAGE_FEATURE_FLAGS)),
) -> VoiceModelState:
    """Switch the live agent's LLM model and republish. Allowlist-validated —
    an unsupported value never reaches Retell."""
    if payload.model not in ALLOWED_VOICE_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"model must be one of {list(ALLOWED_VOICE_MODELS)}.",
        )
    agent_id = await _live_agent_id()
    try:
        agent = await get_agent(agent_id)
        llm_id = (agent.get("response_engine") or {}).get("llm_id")
        if not llm_id:
            raise HTTPException(status_code=409,
                                detail="Agent has no retell-llm engine.")
        before = (await get_llm(llm_id)).get("model")
        await set_llm_model(llm_id, payload.model)
        await publish_agent(agent_id)
    except (RetellNotConfigured, RetellError) as exc:
        raise _retell_http(exc) from exc
    async with _app_db.platform_session_factory() as session:
        await _audit(session, ctx, "admin_set_voice_model",
                     meta={"before": before, "after": payload.model,
                           "agent": agent_id[:24]})
        await session.commit()
    return VoiceModelState(model=payload.model,
                           allowed=list(ALLOWED_VOICE_MODELS), agent_id=agent_id)
