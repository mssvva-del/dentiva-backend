"""RBAC permission-map unit tests (Phase A1).

Pure logic — no DB. Verifies the single source of truth in app/auth/permissions.py:
role hierarchy, deny-by-default, and that the clinic and Dentiva worlds never
share permissions.
"""

from __future__ import annotations

from app.auth import permissions as p


# ── Clinic role hierarchy ───────────────────────────────────────────────────
def test_owner_has_all_clinic_permissions():
    assert p.clinic_permissions_for("owner") == p.CLINIC_PERMISSIONS


def test_viewer_is_read_only():
    perms = p.clinic_permissions_for("viewer")
    assert p.VIEW_DASHBOARD in perms and p.VIEW_ANALYTICS in perms
    # No write / management capabilities.
    for denied in (p.MANAGE_CALLS, p.MANAGE_APPOINTMENTS, p.SEND_SMS,
                   p.MANAGE_SETTINGS, p.MANAGE_TEAM, p.MANAGE_BILLING):
        assert denied not in perms


def test_staff_can_operate_not_configure():
    assert p.has_clinic_permission("staff", p.MANAGE_APPOINTMENTS)
    assert p.has_clinic_permission("staff", p.SEND_SMS)
    assert not p.has_clinic_permission("staff", p.MANAGE_SETTINGS)
    assert not p.has_clinic_permission("staff", p.MANAGE_BILLING)
    assert not p.has_clinic_permission("staff", p.MANAGE_TEAM)


def test_manager_configures_but_no_billing_mgmt_or_team():
    assert p.has_clinic_permission("manager", p.MANAGE_SETTINGS)
    assert p.has_clinic_permission("manager", p.MANAGE_INTEGRATIONS)
    assert p.has_clinic_permission("manager", p.VIEW_BILLING)
    assert not p.has_clinic_permission("manager", p.MANAGE_BILLING)
    assert not p.has_clinic_permission("manager", p.MANAGE_TEAM)


def test_only_owner_manages_billing_and_team():
    assert p.has_clinic_permission("owner", p.MANAGE_BILLING)
    assert p.has_clinic_permission("owner", p.MANAGE_TEAM)


def test_legacy_admin_role_maps_to_owner():
    # v1 used role='admin'; keep those accounts working as owner.
    assert p.clinic_permissions_for("admin") == p.clinic_permissions_for("owner")


def test_unknown_role_has_nothing():
    assert p.clinic_permissions_for("ghost") == frozenset()
    assert not p.has_clinic_permission(None, p.VIEW_DASHBOARD)


# ── Dentiva admin roles ─────────────────────────────────────────────────────
def test_super_admin_has_all_admin_permissions():
    assert p.admin_permissions_for("super_admin") == p.ADMIN_PERMISSIONS


def test_finance_billing_not_operations():
    assert p.has_admin_permission("finance", p.MANAGE_SUBSCRIPTIONS)
    assert p.has_admin_permission("finance", p.VIEW_REVENUE)
    assert not p.has_admin_permission("finance", p.IMPERSONATE_CLINIC)
    assert not p.has_admin_permission("finance", p.MANAGE_DENTIVA_STAFF)


def test_support_can_impersonate_not_bill():
    assert p.has_admin_permission("support", p.IMPERSONATE_CLINIC)
    assert p.has_admin_permission("support", p.VIEW_AUDIT_LOGS)
    assert not p.has_admin_permission("support", p.MANAGE_BILLING_ALL)


def test_sales_sees_leads_not_billing():
    assert p.has_admin_permission("sales", p.MANAGE_LEADS)
    assert not p.has_admin_permission("sales", p.VIEW_REVENUE)


# ── The two worlds never overlap ────────────────────────────────────────────
def test_clinic_and_admin_permission_sets_are_disjoint():
    assert p.CLINIC_PERMISSIONS.isdisjoint(p.ADMIN_PERMISSIONS)


def test_clinic_role_grants_no_admin_permission():
    for role in ("owner", "manager", "staff", "viewer"):
        assert p.clinic_permissions_for(role).isdisjoint(p.ADMIN_PERMISSIONS)


def test_admin_role_grants_no_clinic_permission():
    for role in ("super_admin", "support", "sales", "finance", "engineer"):
        assert p.admin_permissions_for(role).isdisjoint(p.CLINIC_PERMISSIONS)
