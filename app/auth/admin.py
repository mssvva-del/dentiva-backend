"""Admin-world (Dentiva internal) authorization (Phase A2).

Guards `/admin/*` routes. A request reaches admin features ONLY if the user is
`is_internal=True` AND has a `dentiva_staff` role with the required permission.
A clinic user (is_internal=False) always gets 403 here — the two worlds never mix.

Every admin action that touches clinic data must ALSO write an audit_logs row
(HIPAA: cross-tenant PHI access is audited). That happens in the endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy import select

import app.db as _app_db
from app.auth.permissions import has_admin_permission
from app.models.dentiva_staff import DentivaStaff
from app.models.user import User


@dataclass
class AdminContext:
    """Resolved internal caller: the User row + their Dentiva staff role."""

    user: User
    staff_role: str


def require_internal():
    """Dependency: caller must be internal Dentiva staff. Returns AdminContext."""
    from app.dependencies import get_current_user

    async def dependency(user: User = Depends(get_current_user)) -> AdminContext:
        if not user.is_internal:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin area — internal Dentiva staff only.",
            )
        # Reference the factory via the module (not a top-level import) so tests
        # that repoint app.db.async_session_factory take effect here too.
        async with _app_db.async_session_factory() as session:
            staff = (
                await session.execute(
                    select(DentivaStaff).where(DentivaStaff.user_id == user.id)
                )
            ).scalar_one_or_none()
        if staff is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No Dentiva staff role assigned.",
            )
        return AdminContext(user=user, staff_role=staff.role)

    return dependency


def require_admin_permission(permission: str):
    """Dependency: internal staff whose Dentiva role grants ``permission`` (else 403).
    Returns AdminContext.

        @router.get("/api/admin/clinics",
                    dependencies=[Depends(require_admin_permission(VIEW_ALL_CLINICS))])
    """
    internal_dep = require_internal()

    async def dependency(ctx: AdminContext = Depends(internal_dep)) -> AdminContext:
        if not has_admin_permission(ctx.staff_role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Dentiva role '{ctx.staff_role}' lacks '{permission}'.",
            )
        return ctx

    return dependency
