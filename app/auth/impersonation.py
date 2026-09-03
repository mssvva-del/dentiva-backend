"""Read-only "view as clinic" for Dentovox internal staff.

An operator on a support call needs to see what the clinic sees — the same
dashboard, the same calls, the same appointments. Until now the Impersonate
button recorded an audit row and showed a toast, and that was the whole feature:
it never actually opened the clinic's view, so the operator ended up asking the
clinic to read their own screen aloud.

The rule this module enforces is narrow on purpose:

  * only a user who is ``is_internal`` AND holds a ``dentiva_staff`` role
    carrying ``impersonate_clinic`` may do it;
  * reads of anything the clinic reads, plus a short, explicit list of repairs
    a support call actually needs: move or cancel an appointment, and keep the
    note on a patient. Every one of them writes an audit row carrying the STAFF
    user's id, so the trail never reads as the clinic having done it;
  * nothing else. Settings, billing, team, integrations and placing calls as the
    clinic stay refused here — those are done through the admin API under the
    operator's own name. The list is by method and path rather than by verb:
    /api/voice/web-call is a POST behind a view permission and it PLACES A CALL
    as the clinic;
  * only the clinic permissions this module names; the impersonator never
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
    MANAGE_APPOINTMENTS,
    MANAGE_PATIENTS,
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

# Reads that have to be POSTs. Both search on a phone number, and a phone number
# in a URL lands in access logs and browser history — so the query travels in the
# body instead. Refusing them by verb alone broke the clinic's own Calls and
# Patients screens for the operator looking at them, which is the entire feature.
#
# Kept as an explicit list rather than a rule about verbs: /api/voice/web-call is
# also a POST gated by a view permission, and it PLACES A CALL as the clinic.
# Nothing about the method distinguishes the three.
READ_ONLY_POST_PATHS: frozenset[str] = frozenset({
    "/api/calls/search",
    "/api/patients/search",
})


# The repairs a support call needs, named one by one. A clinic rings because an
# appointment is in the wrong place or a patient's note is wrong; sending the
# operator away to "do it in the admin area" is how a test booking sat in a live
# clinic's calendar all morning with nobody able to remove it.
IMPERSONATION_WRITE_PATHS: tuple[tuple[str, str], ...] = (
    ("PATCH", "/api/bookings/"),   # move, amend, cancel, mark no-show
    ("PATCH", "/api/patients/"),   # the note the front desk keeps on a person
)


def _is_read(request: Request) -> bool:
    return request.method == "GET" or (
        request.method == "POST"
        and request.url.path.rstrip("/") in READ_ONLY_POST_PATHS
    )


def _is_allowed_repair(request: Request) -> bool:
    path = request.url.path
    return any(
        request.method == method and path.startswith(prefix)
        for method, prefix in IMPERSONATION_WRITE_PATHS
    )

# What an impersonating operator may see. Deliberately narrower than the clinic
# owner's set: read everything the clinic reads, change nothing.
IMPERSONATION_PERMISSIONS: frozenset[str] = frozenset({
    VIEW_DASHBOARD, VIEW_CALLS, VIEW_APPOINTMENTS,
    VIEW_PATIENTS, VIEW_ANALYTICS, VIEW_BILLING,
    # The two repairs above. Paired with IMPERSONATION_WRITE_PATHS: a permission
    # alone opens every endpoint that asks for it, and "manage appointments" is
    # also what a future endpoint we have not written yet will ask for.
    MANAGE_APPOINTMENTS, MANAGE_PATIENTS,
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
    if not _is_read(request) and not _is_allowed_repair(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "That change isn't available while viewing a clinic. Appointments "
                "and patient notes can be fixed here; everything else is done in "
                "the admin area, under your own name."
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
