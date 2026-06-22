"""RBAC — Role-Based Access Control (Platform Iter 1).

SINGLE SOURCE OF TRUTH for "who can do what". The frontend mirrors this map for
element visibility (`useCan`), but THIS file is the security boundary — every
protected endpoint depends on `require_permission(...)`.

Two completely separate worlds (they never mix — see spec §1):

  • CLINIC world  — dental-practice staff. Scoped to ONE practice (tenant).
        roles: owner > manager > staff > viewer
  • DENTIVA world — our internal team. Cross-tenant (with audit). `is_internal=True`.
        roles: super_admin > support > sales > finance > engineer

Design rules:
  - Permissions are plain string constants (stable identifiers; also sent to the
    frontend). Keep names in sync with API_CONTRACT.md.
  - Role→permission mapping is hard-coded HERE and nowhere else. To change access,
    edit the maps below — never sprinkle role checks across endpoints.
  - A clinic role NEVER grants an admin permission and vice-versa.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status

from app.models.user import User

# ---------------------------------------------------------------------------
# CLINIC permissions (tenant world) — see spec §1 "Permission-матрица (клиника)"
# ---------------------------------------------------------------------------
VIEW_DASHBOARD = "view_dashboard"
VIEW_CALLS = "view_calls"
MANAGE_CALLS = "manage_calls"
VIEW_APPOINTMENTS = "view_appointments"
MANAGE_APPOINTMENTS = "manage_appointments"
VIEW_PATIENTS = "view_patients"
MANAGE_PATIENTS = "manage_patients"
SEND_SMS = "send_sms"
MANAGE_TEMPLATES = "manage_templates"
VIEW_ANALYTICS = "view_analytics"
MANAGE_SETTINGS = "manage_settings"
MANAGE_INTEGRATIONS = "manage_integrations"
MANAGE_TEAM = "manage_team"
MANAGE_BILLING = "manage_billing"
VIEW_BILLING = "view_billing"

CLINIC_PERMISSIONS: frozenset[str] = frozenset({
    VIEW_DASHBOARD, VIEW_CALLS, MANAGE_CALLS,
    VIEW_APPOINTMENTS, MANAGE_APPOINTMENTS,
    VIEW_PATIENTS, MANAGE_PATIENTS,
    SEND_SMS, MANAGE_TEMPLATES,
    VIEW_ANALYTICS,
    MANAGE_SETTINGS, MANAGE_INTEGRATIONS,
    MANAGE_TEAM, MANAGE_BILLING, VIEW_BILLING,
})

# ---------------------------------------------------------------------------
# DENTIVA admin permissions (internal world) — spec §1 "Permission-матрица (admin)"
# ---------------------------------------------------------------------------
VIEW_ALL_CLINICS = "view_all_clinics"
VIEW_CLINIC_DETAIL = "view_clinic_detail"
IMPERSONATE_CLINIC = "impersonate_clinic"
MANAGE_CLINIC_STATUS = "manage_clinic_status"
VIEW_BILLING_ALL = "view_billing_all"
MANAGE_BILLING_ALL = "manage_billing_all"
MANAGE_SUBSCRIPTIONS = "manage_subscriptions"
VIEW_USAGE_METRICS = "view_usage_metrics"
VIEW_REVENUE = "view_revenue"
MANAGE_LEADS = "manage_leads"
MANAGE_DENTIVA_STAFF = "manage_dentiva_staff"
VIEW_SYSTEM_HEALTH = "view_system_health"
MANAGE_FEATURE_FLAGS = "manage_feature_flags"
VIEW_AUDIT_LOGS = "view_audit_logs"

ADMIN_PERMISSIONS: frozenset[str] = frozenset({
    VIEW_ALL_CLINICS, VIEW_CLINIC_DETAIL, IMPERSONATE_CLINIC, MANAGE_CLINIC_STATUS,
    VIEW_BILLING_ALL, MANAGE_BILLING_ALL, MANAGE_SUBSCRIPTIONS,
    VIEW_USAGE_METRICS, VIEW_REVENUE, MANAGE_LEADS, MANAGE_DENTIVA_STAFF,
    VIEW_SYSTEM_HEALTH, MANAGE_FEATURE_FLAGS, VIEW_AUDIT_LOGS,
})

# ---------------------------------------------------------------------------
# Role → permission maps. Hierarchy is expressed by composition (a higher role
# starts from the lower role's set and adds to it) so it stays readable.
# ---------------------------------------------------------------------------

# CLINIC roles
_VIEWER = frozenset({VIEW_DASHBOARD, VIEW_CALLS, VIEW_APPOINTMENTS, VIEW_PATIENTS, VIEW_ANALYTICS})
_STAFF = _VIEWER | {MANAGE_CALLS, MANAGE_APPOINTMENTS, MANAGE_PATIENTS, SEND_SMS}
_MANAGER = _STAFF | {MANAGE_TEMPLATES, MANAGE_SETTINGS, MANAGE_INTEGRATIONS, VIEW_BILLING}
# owner = everything in the clinic world (incl. team + billing management)
_OWNER = _MANAGER | {MANAGE_TEAM, MANAGE_BILLING}

CLINIC_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": _OWNER,
    "manager": _MANAGER,
    "staff": _STAFF,
    "viewer": _VIEWER,
    # legacy value from v1 (pre-RBAC) — treat as owner so existing accounts keep working
    "admin": _OWNER,
}

# DENTIVA admin roles
_ENGINEER = frozenset({VIEW_SYSTEM_HEALTH, MANAGE_FEATURE_FLAGS, VIEW_ALL_CLINICS})
_SALES = frozenset({VIEW_ALL_CLINICS, VIEW_CLINIC_DETAIL, MANAGE_LEADS})
_FINANCE = frozenset({
    VIEW_ALL_CLINICS, VIEW_CLINIC_DETAIL, VIEW_BILLING_ALL, MANAGE_BILLING_ALL,
    MANAGE_SUBSCRIPTIONS, VIEW_USAGE_METRICS, VIEW_REVENUE,
})
_SUPPORT = frozenset({
    VIEW_ALL_CLINICS, VIEW_CLINIC_DETAIL, IMPERSONATE_CLINIC, VIEW_AUDIT_LOGS,
    VIEW_USAGE_METRICS,
})
# super_admin = ALL admin permissions
_SUPER_ADMIN = ADMIN_PERMISSIONS

ADMIN_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "super_admin": _SUPER_ADMIN,
    "support": _SUPPORT,
    "sales": _SALES,
    "finance": _FINANCE,
    "engineer": _ENGINEER,
}


# ---------------------------------------------------------------------------
# Pure helpers (no FastAPI) — easy to unit-test
# ---------------------------------------------------------------------------
def clinic_permissions_for(role: str | None) -> frozenset[str]:
    """All clinic permissions a clinic role has (empty for unknown role)."""
    return CLINIC_ROLE_PERMISSIONS.get(role or "", frozenset())


def admin_permissions_for(role: str | None) -> frozenset[str]:
    """All admin permissions a Dentiva role has (empty for unknown role)."""
    return ADMIN_ROLE_PERMISSIONS.get(role or "", frozenset())


def has_clinic_permission(role: str | None, permission: str) -> bool:
    return permission in clinic_permissions_for(role)


def has_admin_permission(role: str | None, permission: str) -> bool:
    return permission in admin_permissions_for(role)


# ---------------------------------------------------------------------------
# FastAPI dependency factory — the actual security boundary for CLINIC endpoints.
#   usage:  @router.get(..., dependencies=[Depends(require_permission(VIEW_CALLS))])
#   or:     async def handler(user: User = Depends(require_permission(MANAGE_BILLING)))
# Admin-world enforcement (is_internal + dentiva_staff role) is added in Phase A2
# once the columns/table exist (see app/auth/admin.py).
# ---------------------------------------------------------------------------
def require_permission(permission: str):
    """Return a FastAPI dependency that 403s unless the current user's CLINIC role
    grants ``permission``; returns the ``User`` on success.

        @router.get(..., dependencies=[Depends(require_permission(VIEW_CALLS))])
        # or, to use the user object:
        async def handler(user: User = Depends(require_permission(MANAGE_BILLING))): ...

    `get_current_user` is imported lazily inside the closure to avoid a circular
    import (app.dependencies imports from app.auth in later phases).
    """
    # WHY a dependency, not a decorator: it must run inside FastAPI's DI graph so
    # it can depend on get_current_user (which validates the Clerk JWT) and so the
    # 403 fires during request resolution, before the handler body — a decorator
    # couldn't inject the resolved User or compose with the other Depends().
    from app.dependencies import get_current_user

    # WHY user.role: the role lives on OUR users row (set at provisioning), not in
    # the Clerk token — Clerk org roles are mapped to clinic roles once, at login.
    async def dependency(user: User = Depends(get_current_user)) -> User:
        if not has_clinic_permission(user.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Your role ('{user.role}') lacks permission '{permission}'.",
            )
        return user

    return dependency
