"""Read-only "view as clinic" — the access boundary.

The feature exists so an operator on a support call can open the clinic's own
screens. Everything valuable about it is what it REFUSES, so that is what these
tests pin: a clinic user cannot use the header, a write cannot use it, and a
staff role without the permission cannot use it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.db as _app_db
from app.auth import impersonation as imp
from app.models.dentiva_staff import DentivaStaff


def _request(method: str = "GET", view_as: str | None = None):
    headers = {imp.VIEW_AS_HEADER: view_as} if view_as else {}
    return SimpleNamespace(method=method, headers=headers)


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
async def test_writes_are_refused(staff_role, method):
    # A write while impersonating would be recorded as the CLINIC's own change.
    staff_role("super_admin")
    with pytest.raises(HTTPException) as exc:
        await imp.impersonated_practice_id(
            _request(method=method, view_as=str(uuid.uuid4())), _user()
        )
    assert exc.value.status_code == 403
    assert "read-only" in exc.value.detail


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
def test_impersonation_grants_reads_only():
    from app.auth import permissions as p

    assert p.VIEW_DASHBOARD in imp.IMPERSONATION_PERMISSIONS
    assert p.VIEW_CALLS in imp.IMPERSONATION_PERMISSIONS
    assert p.VIEW_PATIENTS in imp.IMPERSONATION_PERMISSIONS
    for denied in (p.MANAGE_APPOINTMENTS, p.MANAGE_SETTINGS, p.MANAGE_TEAM,
                   p.MANAGE_BILLING, p.SEND_SMS, p.MANAGE_INTEGRATIONS):
        assert denied not in imp.IMPERSONATION_PERMISSIONS
    # And it never leaks an admin-world permission into the clinic world.
    assert not (imp.IMPERSONATION_PERMISSIONS & p.ADMIN_PERMISSIONS)
