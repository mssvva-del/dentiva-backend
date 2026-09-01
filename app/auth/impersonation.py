"""Read-only "view as clinic" for Dentovox internal staff.

An operator on a support call needs to see what the clinic sees — the same
dashboard, the same calls, the same appointments. Until now the Impersonate
button recorded an audit row and showed a toast, and that was the whole feature:
it never actually opened the clinic's view, so the operator ended up asking the
clinic to read their own screen aloud.

The rule this module enforces is narrow on purpose:

  * only a user who is ``is_internal`` AND holds a ``dentiva_staff`` role
    carrying ``impersonate_clinic`` may do it;
  * only ``GET`` — a write while impersonating would land in the audit trail as
    the CLINIC having made the change, which is untrue and unfixable after the
    fact. Operators change clinic settings through the admin API, under their own
    name;
  * only the clinic permissions a read-only viewer has; the impersonator never
    inherits the clinic owner's rights.

Signalled by a request header rather than a session flag so it cannot leak into
a background job or a webhook: it is scoped to exactly the request that carries
it.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, Request, status
from sqlalchemy import select

import app.db as _app_db
from app.auth.permissions import (
    IMPERSONATE_CLINIC,
    VIEW_ANALYTICS,
    VIEW_APPOINTMENTS,
    VIEW_BILLING,
    VIEW_CALLS,
    VIEW_DASHBOARD,
    VIEW_PATIENTS,
    has_admin_permission,
)
from app.models.dentiva_staff import DentivaStaff
from app.models.user import User

VIEW_AS_HEADER = "X-Dentovox-View-As"

# What an impersonating operator may see. Deliberately narrower than the clinic
# owner's set: read everything the clinic reads, change nothing.
IMPERSONATION_PERMISSIONS: frozenset[str] = frozenset({
    VIEW_DASHBOARD, VIEW_CALLS, VIEW_APPOINTMENTS,
    VIEW_PATIENTS, VIEW_ANALYTICS, VIEW_BILLING,
})


async def impersonated_practice_id(
    request: Request, user: User
) -> uuid.UUID | None:
    """The practice this request is viewing as, or None for an ordinary request.

    Raises 403 when the header is present but the caller may not use it, so a
    clinic user who discovers the header name gets a refusal rather than another
    tenant's data.
    """
    raw = request.headers.get(VIEW_AS_HEADER)
    if not raw:
        return None

    if not user.is_internal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewing as a clinic is for Dentovox staff only.",
        )
    if request.method != "GET":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Viewing as a clinic is read-only. Make changes through the "
                "admin area so the audit trail names you, not the clinic."
            ),
        )

    async with _app_db.async_session_factory() as session:
        staff = (
            await session.execute(
                select(DentivaStaff).where(DentivaStaff.user_id == user.id)
            )
        ).scalar_one_or_none()
    if staff is None or not has_admin_permission(staff.role, IMPERSONATE_CLINIC):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your Dentovox role cannot view a clinic's own screens.",
        )

    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{VIEW_AS_HEADER} must be a practice id.",
        ) from exc
