"""
Every role rule in ONE place (PLAN.md rule 11).

Views never compare role strings. They ask for a capability, either as a DRF permission class
(`Allow("tank_records")`) or inline (`require(user, "manage_shift")` / `can(user, ...)`).
Hiding a button in the app is never the protection; this module is.

Tank side (dips, deliveries, tank levels, reconciliation) is for managers and owners only. The person
who sells fuel does not also measure what is left in the tank, so attendants are refused even read
access, and `strip_restricted` removes tank fields from anything an attendant receives.
"""
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Role

O, M, A, P = Role.OWNER, Role.MANAGER, Role.ATTENDANT, Role.PLATFORM_ADMIN

CAPABILITIES = {
    # control plane (Tendeka & Elichi)
    "platform": {P},
    # people, settings, billing
    "manage_users": {O},
    "view_users": {O, M},
    "manage_settings": {O},
    "view_settings": {O, M},
    "view_billing": {O, M},
    "owner": {O},  # owner-only screens: audit log, exports, daily-summary settings, billing actions
    # station setup
    "manage_stations": {O},  # add or remove a station, assign staff to stations
    "edit_setup": {O, M},  # tanks, pumps, nozzles, prices, exchange rates, calibration, photos, QR images
    "view_setup": {O, M, A},  # stations, pumps, nozzles, prices (what an attendant needs to enter meters)
    # forecourt entry
    "record_shift": {O, M, A},  # meter numbers and one's own cash
    "manage_shift": {O, M},  # review, close for others, mark someone as left, assign pumps, resolve readings
    "see_all_shifts": {O, M},
    # tank side: never attendants (rule 11)
    "tank_records": {O, M},  # dips and deliveries, create and read
    "view_tank_levels": {O, M},
    "view_reconciliation": {O, M},
    "view_reports": {O, M},
    "view_dashboard": {O, M},
    "manage_alerts": {O, M},
    "onboarding": {O, M},
}

# When a tenant is suspended these roles are locked behind "pay to reactivate"; attendants keep recording.
LOCKED_WHEN_SUSPENDED = {O, M}

# Never sent to an attendant, wherever they appear in a response.
TANK_FIELDS = {"tanks", "tank_levels", "last_litres", "current_litres", "level_pct", "capacity_litres", "reserve_litres",
               "has_calibration", "dips", "deliveries", "reconciliation", "days_to_empty", "refill_by"}


def can(user, capability):
    if capability not in CAPABILITIES:
        raise KeyError(f"Unknown capability '{capability}'")
    return bool(user and getattr(user, "is_authenticated", False) and user.role in CAPABILITIES[capability])


def require(user, capability, message=None):
    if not can(user, capability):
        raise PermissionDenied(message or "You do not have access to this.")


def sees_all_stations(user):
    return user.role == O


def strip_restricted(user, data):
    """Remove tank-side fields (recursively) from a response for roles that may not see tank levels."""
    if can(user, "view_tank_levels"):
        return data
    if isinstance(data, dict):
        return {k: strip_restricted(user, v) for k, v in data.items() if k not in TANK_FIELDS}
    if isinstance(data, list):
        return [strip_restricted(user, v) for v in data]
    return data


def Allow(capability, *, read=None):  # noqa: N802 - reads like a class at the use site
    """DRF permission class: `capability` for every method, or `read` for GET/HEAD/OPTIONS when given."""

    class _Allow(BasePermission):
        def has_permission(self, request, view):
            cap = read if (read and request.method in SAFE_METHODS) else capability
            return can(request.user, cap)

    _Allow.__name__ = f"Allow_{capability}"
    return _Allow


# Named classes kept for existing views.
IsPlatformAdmin = Allow("platform")
IsOwner = Allow("owner")
IsOwnerOrManager = Allow("view_dashboard")
IsTenantUser = Allow("record_shift")
OwnerWriteManagerRead = Allow("manage_users", read="view_users")
TankRecords = Allow("tank_records")
SetupPermission = Allow("edit_setup", read="view_setup")
