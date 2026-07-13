"""Onboarding wizard endpoints (Platform Iter 1, Phase B2).

A self-serve, resumable wizard that an owner runs once to take a freshly
provisioned practice from `status='onboarding'` to `status='active'`. Progress is
saved on `practices.onboarding_step` after every step, so the wizard can be
interrupted and resumed exactly where it left off.

  GET  /api/onboarding/state    → current step + saved values (resume)
  PUT  /api/onboarding/clinic   → step 1: name, address, timezone
  PUT  /api/onboarding/hours    → step 2: business_hours
  PUT  /api/onboarding/phone    → step 3: forward existing number / skip
  PUT  /api/onboarding/pms      → step 4: Open Dental / NexHealth / skip
  PUT  /api/onboarding/agent    → step 5: name, voice, greeting, languages
  POST /api/onboarding/complete → step 6→live: validate, status=active, step=0

AUTHZ: every mutating route requires MANAGE_SETTINGS (owner/manager). Billing
(step 6) is intentionally NOT here — Stripe lands in Phase D and pilots are set
manually by super_admin, so 'complete' activates the practice operationally and
billing is reconciled separately.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import MANAGE_SETTINGS, require_permission
from app.dependencies import get_current_practice, get_tenant_db
from app.models.audit_log import AuditLog
from app.models.baa_acceptance import BaaAcceptance
from app.models.practice import Practice
from app.models.user import User
from app.schemas.onboarding import (
    AgentStep,
    ClinicStep,
    HoursStep,
    OnboardingState,
    PhoneStep,
    PmsStep,
)
from app.services.legal.baa import BAA_VERSION, current_baa

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _load(db: AsyncSession, practice_id: uuid.UUID) -> Practice:
    """Re-fetch the practice inside the tenant-bound session for mutation."""
    return (
        await db.execute(select(Practice).where(Practice.id == practice_id))
    ).scalar_one()


def _advance(practice: Practice, completed_step: int) -> None:
    """Move the wizard forward monotonically after a step is saved.

    Only advances while still onboarding (never drags a live practice back into
    the wizard). onboarding_step holds the NEXT step to do; it only ever
    increases, so re-saving an earlier step doesn't rewind progress.
    """
    if practice.status == "onboarding":
        practice.onboarding_step = max(practice.onboarding_step, completed_step + 1)


async def _audit(db: AsyncSession, practice: Practice, user: User, action: str,
                 meta: dict) -> None:
    db.add(
        AuditLog(
            id=uuid.uuid4(),
            practice_id=practice.id,
            user_id=user.id,
            action=action,
            resource_type="practice",
            resource_id=practice.id,
            audit_metadata=meta,
        )
    )


def _state(practice: Practice) -> OnboardingState:
    from app.config import get_settings
    from app.services.call_routing import forwarding_instruction

    settings = get_settings()
    return OnboardingState(
        practice_id=str(practice.id),
        status=practice.status,
        onboarding_step=practice.onboarding_step,
        complete=practice.onboarding_step == 0,
        name=practice.name,
        address=practice.address,
        timezone=practice.timezone,
        business_hours=practice.business_hours,
        phone_number=practice.phone_number,
        transfer_phone_number=practice.transfer_phone_number,
        pms_system=practice.pms_system,
        languages_enabled=list(practice.languages_enabled),
        agent_settings=practice.agent_settings,
        ai_phone_number=settings.retell_from_number or None,
        forwarding_instruction=forwarding_instruction(
            answer_mode=practice.answer_mode,
            rings_before_ai=practice.rings_before_ai,
            ai_number=settings.retell_from_number or None,
        ),
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
@router.get("/state", response_model=OnboardingState)
async def get_state(
    practice: Practice = Depends(get_current_practice),
) -> OnboardingState:
    """Current wizard progress + saved values so the UI can resume pre-filled."""
    return _state(practice)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
@router.put("/clinic", response_model=OnboardingState)
async def step_clinic(
    payload: ClinicStep,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> OnboardingState:
    p = await _load(db, practice.id)
    p.name = payload.name
    p.address = payload.address
    p.timezone = payload.timezone
    _advance(p, 1)
    await _audit(db, p, user, "onboarding_step", {"step": 1, "name": "clinic"})
    await db.commit()
    await db.refresh(p)
    return _state(p)


@router.put("/hours", response_model=OnboardingState)
async def step_hours(
    payload: HoursStep,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> OnboardingState:
    p = await _load(db, practice.id)
    # Serialize the validated pydantic day models back to plain JSON for JSONB.
    p.business_hours = {
        day: (None if v is None else {"open": v.open, "close": v.close})
        for day, v in payload.business_hours.items()
    }
    _advance(p, 2)
    await _audit(db, p, user, "onboarding_step", {"step": 2, "name": "hours"})
    await db.commit()
    await db.refresh(p)
    return _state(p)


@router.put("/phone", response_model=OnboardingState)
async def step_phone(
    payload: PhoneStep,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> OnboardingState:
    p = await _load(db, practice.id)
    # forward → store the number we forward TO; skip → web-call only (clear it).
    if payload.mode == "forward":
        if not payload.forward_number:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="forward_number is required when mode='forward'.",
            )
        p.phone_number = payload.forward_number
    else:
        p.phone_number = None
    # Emergency/live-handoff transfer destination. Independent of mode: a clinic
    # may skip phone forwarding but still want transfer_to_human to reach a real
    # person. Only overwrite when explicitly provided so re-saving the step
    # without it doesn't wipe a previously set number.
    if payload.transfer_number is not None:
        p.transfer_phone_number = payload.transfer_number
    _advance(p, 3)
    await _audit(db, p, user, "onboarding_step",
                 {"step": 3, "name": "phone", "mode": payload.mode,
                  "transfer_set": payload.transfer_number is not None})
    await db.commit()
    await db.refresh(p)
    return _state(p)


@router.put("/pms", response_model=OnboardingState)
async def step_pms(
    payload: PmsStep,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> OnboardingState:
    p = await _load(db, practice.id)
    p.pms_system = payload.pms_system
    _advance(p, 4)
    await _audit(db, p, user, "onboarding_step",
                 {"step": 4, "name": "pms", "pms_system": payload.pms_system})
    await db.commit()
    await db.refresh(p)
    return _state(p)


@router.put("/agent", response_model=OnboardingState)
async def step_agent(
    payload: AgentStep,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> OnboardingState:
    p = await _load(db, practice.id)
    p.languages_enabled = list(payload.languages)
    # Captured for when agents are parameterized per-practice (not wired yet).
    p.agent_settings = {
        "agent_name": payload.agent_name,
        "voice": payload.voice,
        "greeting": payload.greeting,
    }
    _advance(p, 5)
    await _audit(db, p, user, "onboarding_step", {"step": 5, "name": "agent"})
    await db.commit()
    await db.refresh(p)
    return _state(p)


# ---------------------------------------------------------------------------
# BAA / Terms acceptance (ONB-BAA) — HIPAA requires a signed BAA before ANY PHI
# flows. Click-through e-signature; the go-live gate below enforces it.
# ---------------------------------------------------------------------------
class BaaDocument(BaseModel):
    version: str
    text: str
    accepted: bool          # has THIS practice accepted the current version?


class BaaAccept(BaseModel):
    signer_name: str = Field(min_length=1, max_length=200)
    signer_title: str = Field(min_length=1, max_length=200)


def _client_ip(request: Request) -> str | None:
    """Real client IP for the e-signature record. Behind the Railway proxy the
    direct peer (request.client.host) is the proxy, so prefer the FIRST hop in
    X-Forwarded-For (the original client) when present."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:100]
    return request.client.host if request.client else None


async def _has_current_baa(db: AsyncSession, practice_id: uuid.UUID) -> bool:
    return (await db.execute(
        select(func.count()).select_from(BaaAcceptance).where(
            BaaAcceptance.practice_id == practice_id,
            BaaAcceptance.document_version == BAA_VERSION,
        )
    )).scalar_one() > 0


@router.get("/baa", response_model=BaaDocument)
async def get_baa(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> BaaDocument:
    """Current Terms + BAA text/version, and whether this practice already signed it."""
    doc = current_baa()
    return BaaDocument(
        version=doc["version"], text=doc["text"],
        accepted=await _has_current_baa(db, practice.id),
    )


@router.post("/baa/accept", response_model=OnboardingState)
async def accept_baa(
    payload: BaaAccept,
    request: Request,
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> OnboardingState:
    """Record a click-through acceptance of the CURRENT Terms + BAA version.

    Idempotent per version: re-accepting the same version is a no-op (no duplicate
    row). Captures IP + user-agent server-side as ESIGN evidence — never trusts a
    client-supplied value for those."""
    p = await _load(db, practice.id)
    if not await _has_current_baa(db, practice.id):
        # Race-safe: the check above + INSERT aren't atomic, so a double-submit
        # relies on the (practice_id, document_version) UNIQUE index + ON CONFLICT
        # DO NOTHING to collapse into exactly one signature row.
        stmt = (
            pg_insert(BaaAcceptance)
            .values(
                id=uuid.uuid4(), practice_id=p.id, document_version=BAA_VERSION,
                signer_name=payload.signer_name.strip(),
                signer_title=payload.signer_title.strip(),
                signer_ip=_client_ip(request),
                # Full UA truncated to 500 chars — enough for evidence, bounded.
                user_agent=(request.headers.get("user-agent") or "")[:500] or None,
                accepted_by=user.id,
            )
            .on_conflict_do_nothing(
                index_elements=["practice_id", "document_version"]
            )
        )
        await db.execute(stmt)
        await _audit(db, p, user, "baa_accepted",
                     {"version": BAA_VERSION, "signer": payload.signer_name.strip()})
        await db.commit()
        await db.refresh(p)
    return _state(p)


@router.post("/complete", response_model=OnboardingState)
async def complete(
    practice: Practice = Depends(get_current_practice),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_permission(MANAGE_SETTINGS)),
) -> OnboardingState:
    """Finalize onboarding → practice goes live (status='active', step=0).

    Validates that the required steps were completed; refuses to activate a
    half-built practice. Idempotent: completing an already-live practice is a
    no-op success.
    """
    p = await _load(db, practice.id)
    if p.status != "onboarding" and p.onboarding_step == 0:
        return _state(p)  # already live — idempotent

    # HIPAA hard gate: no signed current-version BAA → no go-live. PHI must never
    # flow before the Business Associate Agreement is accepted.
    if not await _has_current_baa(db, practice.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Terms & BAA must be accepted before going live.",
        )

    # Required-field gate (billing intentionally excluded — Phase D / pilot).
    problems: list[str] = []
    if not p.name.strip():
        problems.append("clinic name")
    if not p.business_hours or all(v is None for v in p.business_hours.values()):
        problems.append("business hours")
    if not p.languages_enabled:
        problems.append("languages")
    if not p.agent_settings:
        problems.append("agent setup")
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Cannot go live — missing: {', '.join(problems)}.",
        )

    p.status = "active"
    p.onboarding_step = 0
    await _audit(db, p, user, "onboarding_completed", {"went_live": True})
    await db.commit()
    await db.refresh(p)
    return _state(p)
