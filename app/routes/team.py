"""Team management endpoints (Platform Iter 1, Phase B3).

Lets an owner/manager invite teammates, see members + pending invites, change
roles, and remove people — all scoped to their own practice and gated by
MANAGE_TEAM.

  GET    /api/team/members              → users in the practice
  GET    /api/team/invitations          → pending/historical invites
  POST   /api/team/invitations          → invite (email + role)
  POST   /api/team/invitations/{id}/revoke
  PATCH  /api/team/members/{user_id}     → change role
  DELETE /api/team/members/{user_id}     → remove (soft-disable)

SAFETY:
  * The "last owner" guard refuses any change that would leave the practice with
    zero active owners (can't demote/remove the sole owner — including yourself).
  * Removal is a SOFT disable (status='disabled'), never a hard delete, so audit
    trails and historical ownership stay intact.
  * Invitations are tenant-isolated (RLS) + every query also filters practice_id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import MANAGE_TEAM, require_permission
from app.dependencies import get_current_practice, get_tenant_db
from app.models.audit_log import AuditLog
from app.models.invitation import Invitation
from app.models.practice import Practice
from app.models.user import User
from app.schemas.team import (
    InvitationOut,
    InviteRequest,
    MemberOut,
    RoleUpdateRequest,
)
from app.services.clerk_api import (
    clinic_role_to_clerk_role,
    create_org_invitation,
    revoke_org_invitation,
)

router = APIRouter(prefix="/api/team", tags=["team"])

_INVITE_TTL_DAYS = 7


async def _active_owner_count(db: AsyncSession, practice_id: uuid.UUID) -> int:
    """How many active owners the practice has (for the last-owner guard)."""
    return (
        await db.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.practice_id == practice_id,
                User.role == "owner",
                User.status == "active",
            )
        )
    ).scalar_one()


async def _audit(db, practice, user, action, meta):
    db.add(
        AuditLog(
            id=uuid.uuid4(),
            practice_id=practice.id,
            user_id=user.id,
            action=action,
            resource_type="team",
            resource_id=practice.id,
            audit_metadata=meta,
        )
    )


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
@router.get("/members", response_model=list[MemberOut])
async def list_members(
    practice: Practice = Depends(get_current_practice),
    caller: User = Depends(require_permission(MANAGE_TEAM)),
    db: AsyncSession = Depends(get_tenant_db),
) -> list[MemberOut]:
    rows = (
        await db.execute(
            select(User)
            .where(User.practice_id == practice.id)
            .order_by(User.created_at)
        )
    ).scalars().all()
    return [
        MemberOut(
            id=str(u.id), email=u.email, role=u.role, status=u.status,
            is_self=(u.id == caller.id),
        )
        for u in rows
    ]


@router.patch("/members/{user_id}", response_model=MemberOut)
async def update_member_role(
    user_id: uuid.UUID,
    payload: RoleUpdateRequest,
    practice: Practice = Depends(get_current_practice),
    caller: User = Depends(require_permission(MANAGE_TEAM)),
    db: AsyncSession = Depends(get_tenant_db),
) -> MemberOut:
    target = (
        await db.execute(
            select(User).where(User.id == user_id, User.practice_id == practice.id)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Member not found.")

    # Last-owner guard: don't demote the only owner.
    if target.role == "owner" and payload.role != "owner":
        if await _active_owner_count(db, practice.id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot remove the practice's last owner. Assign another owner first.",
            )

    old_role = target.role
    target.role = payload.role
    await _audit(db, practice, caller, "team_role_changed",
                 {"target": str(target.id), "from": old_role, "to": payload.role})
    await db.commit()
    await db.refresh(target)
    return MemberOut(
        id=str(target.id), email=target.email, role=target.role,
        status=target.status, is_self=(target.id == caller.id),
    )


@router.delete("/members/{user_id}", status_code=status.HTTP_200_OK)
async def remove_member(
    user_id: uuid.UUID,
    practice: Practice = Depends(get_current_practice),
    caller: User = Depends(require_permission(MANAGE_TEAM)),
    db: AsyncSession = Depends(get_tenant_db),
) -> dict:
    target = (
        await db.execute(
            select(User).where(User.id == user_id, User.practice_id == practice.id)
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="Member not found.")

    # Last-owner guard (covers removing yourself when you're the sole owner).
    if target.role == "owner" and await _active_owner_count(db, practice.id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot remove the practice's last owner. Assign another owner first.",
        )

    # Soft disable — never hard delete (audit + historical integrity).
    target.status = "disabled"
    await _audit(db, practice, caller, "team_member_removed", {"target": str(target.id)})
    await db.commit()
    return {"status": "ok", "removed": str(target.id)}


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
@router.get("/invitations", response_model=list[InvitationOut])
async def list_invitations(
    practice: Practice = Depends(get_current_practice),
    _: User = Depends(require_permission(MANAGE_TEAM)),
    db: AsyncSession = Depends(get_tenant_db),
) -> list[InvitationOut]:
    rows = (
        await db.execute(
            select(Invitation)
            .where(Invitation.practice_id == practice.id)
            .order_by(Invitation.created_at.desc())
        )
    ).scalars().all()
    return [
        InvitationOut(
            id=str(i.id), email=i.email, role=i.role, status=i.status,
            created_at=i.created_at, expires_at=i.expires_at,
        )
        for i in rows
    ]


@router.post("/invitations", response_model=InvitationOut, status_code=201)
async def create_invitation(
    payload: InviteRequest,
    practice: Practice = Depends(get_current_practice),
    caller: User = Depends(require_permission(MANAGE_TEAM)),
    db: AsyncSession = Depends(get_tenant_db),
) -> InvitationOut:
    email = payload.email  # already normalized (lowercased) by the schema

    # Block re-inviting someone who's already an active member.
    existing_member = (
        await db.execute(
            select(User).where(
                User.practice_id == practice.id,
                func.lower(User.email) == email,
                User.status == "active",
            )
        )
    ).scalar_one_or_none()
    if existing_member is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That person is already a member of this practice.",
        )

    # Block a duplicate pending invite (also enforced by a partial unique index).
    dup = (
        await db.execute(
            select(Invitation).where(
                Invitation.practice_id == practice.id,
                func.lower(Invitation.email) == email,
                Invitation.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="There's already a pending invitation for that email.",
        )

    # Best-effort Clerk invitation (no-op without a secret; never raises).
    clerk_invitation_id = await create_org_invitation(
        clerk_org_id=practice.clerk_org_id,
        email=email,
        clerk_role=clinic_role_to_clerk_role(payload.role),
    )

    inv = Invitation(
        id=uuid.uuid4(),
        practice_id=practice.id,
        email=email,
        role=payload.role,
        status="pending",
        clerk_invitation_id=clerk_invitation_id,
        invited_by=caller.id,
        expires_at=datetime.now(UTC) + timedelta(days=_INVITE_TTL_DAYS),
    )
    db.add(inv)
    await _audit(db, practice, caller, "team_invited",
                 {"email": email, "role": payload.role})
    await db.commit()
    await db.refresh(inv)
    return InvitationOut(
        id=str(inv.id), email=inv.email, role=inv.role, status=inv.status,
        created_at=inv.created_at, expires_at=inv.expires_at,
    )


@router.post("/invitations/{invitation_id}/revoke", response_model=InvitationOut)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    practice: Practice = Depends(get_current_practice),
    caller: User = Depends(require_permission(MANAGE_TEAM)),
    db: AsyncSession = Depends(get_tenant_db),
) -> InvitationOut:
    inv = (
        await db.execute(
            select(Invitation).where(
                Invitation.id == invitation_id,
                Invitation.practice_id == practice.id,
            )
        )
    ).scalar_one_or_none()
    if inv is None:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    if inv.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invitation is already {inv.status}.",
        )

    if inv.clerk_invitation_id:
        await revoke_org_invitation(
            clerk_org_id=practice.clerk_org_id,
            clerk_invitation_id=inv.clerk_invitation_id,
        )
    inv.status = "revoked"
    await _audit(db, practice, caller, "team_invite_revoked", {"invitation": str(inv.id)})
    await db.commit()
    await db.refresh(inv)
    return InvitationOut(
        id=str(inv.id), email=inv.email, role=inv.role, status=inv.status,
        created_at=inv.created_at, expires_at=inv.expires_at,
    )
