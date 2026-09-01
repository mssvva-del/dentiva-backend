"""Current-user identity + resolved permissions (Platform Iter 1, RBAC).

The frontend calls GET /api/me once after login and uses the returned
`permissions` list to drive `useCan()` (element visibility + route guards).
The role→permission mapping lives ONLY in app/auth/permissions.py — the frontend
never duplicates it, it just consumes this list. Security is still enforced
server-side on every endpoint via require_permission().
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select

import app.db as _app_db
from app.auth.impersonation import (
    IMPERSONATION_PERMISSIONS,
    impersonated_practice_id,
)
from app.auth.permissions import admin_permissions_for, clinic_permissions_for
from app.dependencies import get_current_user
from app.models.dentiva_staff import DentivaStaff
from app.models.user import User

router = APIRouter(prefix="/api", tags=["me"])


class MeResponse(BaseModel):
    id: str
    email: str
    is_internal: bool
    practice_id: str | None
    role: str                 # clinic role (or '' for pure-internal users)
    staff_role: str | None    # Dentiva role when is_internal
    permissions: list[str]    # resolved permission strings for the caller's world
    # Set only while Dentovox staff are viewing this clinic read-only; the
    # dashboard uses it to show the 'viewing as' banner and the way out.
    viewing_as_practice_id: str | None = None


@router.get("/me", response_model=MeResponse)
async def get_me(
    request: Request, user: User = Depends(get_current_user)
) -> MeResponse:
    """Identity + the exact permissions this caller has, for the frontend."""
    staff_role: str | None = None
    practice_id = user.practice_id
    # Staff viewing a clinic get the clinic's read-only screens, not the admin
    # ones: without this the dashboard hides every clinic page from them, since
    # useCan() reads exactly this list.
    viewing_as = await impersonated_practice_id(request, user)
    if viewing_as is not None:
        practice_id = viewing_as
        permissions = sorted(IMPERSONATION_PERMISSIONS)
    elif user.is_internal:
        async with _app_db.async_session_factory() as session:
            staff = (
                await session.execute(
                    select(DentivaStaff).where(DentivaStaff.user_id == user.id)
                )
            ).scalar_one_or_none()
        staff_role = staff.role if staff else None
        permissions = sorted(admin_permissions_for(staff_role))
    else:
        permissions = sorted(clinic_permissions_for(user.role))

    return MeResponse(
        id=str(user.id),
        email=user.email,
        is_internal=user.is_internal,
        practice_id=str(practice_id) if practice_id else None,
        role=user.role if not user.is_internal else "",
        staff_role=staff_role,
        permissions=permissions,
        viewing_as_practice_id=str(viewing_as) if viewing_as else None,
    )
