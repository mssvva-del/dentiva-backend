""""View as clinic" — the access boundary.

The feature exists so an operator on a support call can open the clinic's own
screens, and fix the two things a support call is actually about: an appointment
in the wrong place, and the note kept on a patient. Everything else valuable
about it is what it REFUSES, so that is what these tests pin: a clinic user
cannot use the header, a staff role without the permission cannot use it, and no
other write gets through — settings, billing, team, or placing a call as the
clinic.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.db as _app_db
from app.auth import impersonation as imp
from app.models.dentiva_staff import DentivaStaff


def _request(method: str = "GET", view_as: str | None = None, path: str = "/api/calls"):
    headers = {imp.VIEW_AS_HEADER: view_as} if view_as else {}
    return SimpleNamespace(
        method=method, headers=headers, url=SimpleNamespace(path=path)
    )


def _user(*, internal: bool = True):
    return SimpleNamespace(id=uuid.uuid4(), is_internal=internal, role="owner")


class _FakeSession:
    """Stands in for the DB: returns whatever staff row the test wants."""

    def __init__(self, staff):
        self._staff = staff

    async def execute(self, _stmt):
        staff = self._staff
        return SimpleNamespace(scalar_one_or_none=lambda: staff)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def staff_role(monkeypatch):
    """Install a staff row with the given role (None = no staff row at all)."""

    def _install(role: str | None):
        staff = (
            DentivaStaff(id=uuid.uuid4(), user_id=uuid.uuid4(), role=role)
            if role
            else None
        )
        monkeypatch.setattr(
            _app_db, "async_session_factory", lambda: _FakeSession(staff)
        )

    return _install


# ── The ordinary case ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_header_means_no_impersonation(staff_role):
    staff_role("super_admin")
    assert await imp.impersonated_practice_id(_request(), _user()) is None


@pytest.mark.asyncio
async def test_super_admin_may_view_a_clinic(staff_role):
    staff_role("super_admin")
    target = uuid.uuid4()
    got = await imp.impersonated_practice_id(
        _request(view_as=str(target)), _user()
    )
    assert got == target


@pytest.mark.asyncio
async def test_support_may_view_a_clinic(staff_role):
    # Support is the role that actually sits on the phone with a clinic.
    staff_role("support")
    target = uuid.uuid4()
    assert await imp.impersonated_practice_id(
        _request(view_as=str(target)), _user()
    ) == target


# ── What it refuses ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_clinic_user_cannot_use_the_header(staff_role):
    # The whole tenant boundary rests on this one: knowing the header name must
    # not be enough to read another practice's patients.
    staff_role("super_admin")
    with pytest.raises(HTTPException) as exc:
        await imp.impersonated_practice_id(
            _request(view_as=str(uuid.uuid4())), _user(internal=False)
        )
    assert exc.value.status_code == 403


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
@pytest.mark.asyncio
async def test_writes_outside_the_two_repairs_are_refused(staff_role, method):
    # Anything not on the repair list is still done in the admin area, under the
    # operator's own name.
    staff_role("super_admin")
    with pytest.raises(HTTPException) as exc:
        await imp.impersonated_practice_id(
            _request(method=method, view_as=str(uuid.uuid4()),
                     path="/api/practice/settings"),
            _user(),
        )
    assert exc.value.status_code == 403
    assert "admin area" in exc.value.detail


@pytest.mark.parametrize("path", [
    "/api/bookings/8b0f2c1e-0000-0000-0000-000000000000",
    "/api/bookings/8b0f2c1e-0000-0000-0000-000000000000/status",
    "/api/patients/8b0f2c1e-0000-0000-0000-000000000000",
])
@pytest.mark.asyncio
async def test_the_two_repairs_a_support_call_needs_go_through(staff_role, path):
    """A test booking sat in a live clinic's calendar all morning because the
    only person looking at it was viewing the clinic, and viewing could not
    cancel. Both write an audit row carrying the STAFF user's id."""
    staff_role("super_admin")
    target = uuid.uuid4()
    got = await imp.impersonated_practice_id(
        _request(method="PATCH", view_as=str(target), path=path), _user()
    )
    assert got == target


@pytest.mark.asyncio
async def test_a_repair_path_on_another_verb_is_still_refused(staff_role):
    """DELETE /api/bookings/{id} is not "fix the schedule" — it is erasing the
    record of it."""
    staff_role("super_admin")
    with pytest.raises(HTTPException) as exc:
        await imp.impersonated_practice_id(
            _request(method="DELETE", view_as=str(uuid.uuid4()),
                     path="/api/bookings/8b0f2c1e-0000-0000-0000-000000000000"),
            _user(),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_staff_role_without_the_permission_is_refused(staff_role):
    # finance sees money, not a clinic's patient screens.
    staff_role("finance")
    with pytest.raises(HTTPException) as exc:
        await imp.impersonated_practice_id(
            _request(view_as=str(uuid.uuid4())), _user()
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_internal_user_with_no_staff_row_is_refused(staff_role):
    staff_role(None)
    with pytest.raises(HTTPException) as exc:
        await imp.impersonated_practice_id(
            _request(view_as=str(uuid.uuid4())), _user()
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_garbage_practice_id_is_a_400_not_a_500(staff_role):
    staff_role("super_admin")
    with pytest.raises(HTTPException) as exc:
        await imp.impersonated_practice_id(
            _request(view_as="not-a-uuid"), _user()
        )
    assert exc.value.status_code == 400


# ── The permission set itself ───────────────────────────────────────────────
def test_impersonation_grants_reads_and_the_two_repairs():
    from app.auth import permissions as p

    assert p.VIEW_DASHBOARD in imp.IMPERSONATION_PERMISSIONS
    assert p.VIEW_CALLS in imp.IMPERSONATION_PERMISSIONS
    assert p.VIEW_PATIENTS in imp.IMPERSONATION_PERMISSIONS
    # Fixing a schedule and a patient note is what a support call is about; the
    # path list above is what keeps these two from opening anything else.
    assert p.MANAGE_APPOINTMENTS in imp.IMPERSONATION_PERMISSIONS
    assert p.MANAGE_PATIENTS in imp.IMPERSONATION_PERMISSIONS
    for denied in (p.MANAGE_SETTINGS, p.MANAGE_TEAM, p.MANAGE_BILLING,
                   p.SEND_SMS, p.MANAGE_INTEGRATIONS, p.MANAGE_CALLS):
        assert denied not in imp.IMPERSONATION_PERMISSIONS
    # And it never leaks an admin-world permission into the clinic world.
    assert not (imp.IMPERSONATION_PERMISSIONS & p.ADMIN_PERMISSIONS)


@pytest.mark.parametrize("path", ["/api/calls/search", "/api/patients/search"])
@pytest.mark.asyncio
async def test_a_search_is_a_read_even_though_it_is_a_post(staff_role, path):
    """These are POSTs only because they search on a phone number, and a phone
    number in a URL lands in access logs and browser history.

    Refusing them by verb broke the clinic's own Calls and Patients screens for
    the operator looking at them — which is the entire feature. The screen said
    "Couldn't load data" and gave no hint that impersonation was the cause.
    """
    staff_role("super_admin")
    target = uuid.uuid4()
    got = await imp.impersonated_practice_id(
        _request(method="POST", view_as=str(target), path=path), _user()
    )
    assert got == target


@pytest.mark.asyncio
async def test_placing_a_call_is_not_a_read_however_it_is_gated(staff_role):
    """/api/voice/web-call is also a POST behind a view permission, and it puts a
    live call out as that clinic. Nothing about the method separates it from the
    two searches, which is why the allow-list is a list and not a rule."""
    staff_role("super_admin")
    with pytest.raises(HTTPException) as exc:
        await imp.impersonated_practice_id(
            _request(method="POST", view_as=str(uuid.uuid4()),
                     path="/api/voice/web-call"),
            _user(),
        )
    assert exc.value.status_code == 403
