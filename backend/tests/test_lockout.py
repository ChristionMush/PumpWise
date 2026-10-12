"""Suspension and cancellation (Phase 2 switch; automated billing arrives in Phase 8)."""
from rest_framework.test import APIClient

from apps.accounts.models import Role

from .helpers import PASSWORD, client_for, make_station, two_tenants


def set_status(a, status):
    from .helpers import platform_client

    res = platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/", {"status": status}, format="json")
    assert res.status_code == 200


def test_suspended_tenant_locks_owner_and_manager_but_not_attendants():
    a, b = two_tenants()
    make_station(a["tenant"], "A1")
    set_status(a, "suspended")
    for role in (Role.OWNER, Role.MANAGER):
        c = client_for(a[role])
        for url in ("/api/users/", "/api/stations/", "/api/audit/"):
            res = c.get(url)
            assert res.status_code == 403 and res.json()["code"] == "tenant_suspended", (role, url)
        # ...but the app can still show the "pay to reactivate" screen
        assert c.get("/api/auth/me/").json()["tenant"]["status"] == "suspended"
        assert c.get("/api/subscription/").status_code == 200
    assert client_for(a[Role.ATTENDANT]).get("/api/stations/").status_code == 200  # grace: records keep flowing
    assert client_for(b[Role.OWNER]).get("/api/users/").status_code == 200  # other tenants unaffected


def test_suspended_owner_can_still_log_in_to_see_the_lock_screen():
    a, _ = two_tenants()
    set_status(a, "suspended")
    res = APIClient().post("/api/auth/login/", {"identifier": a[Role.OWNER].email, "password": PASSWORD}, format="json")
    assert res.status_code == 200


def test_reactivation_is_instant():
    a, _ = two_tenants()
    c = client_for(a[Role.OWNER])
    set_status(a, "suspended")
    assert c.get("/api/users/").status_code == 403
    set_status(a, "active")
    assert c.get("/api/users/").status_code == 200


def test_cancelled_tenant_locks_everyone_out():
    a, _ = two_tenants()
    set_status(a, "cancelled")
    for role in (Role.OWNER, Role.MANAGER, Role.ATTENDANT):
        assert client_for(a[role]).get("/api/stations/").status_code == 403


def test_my_subscription_is_own_only_read_only_and_closed_to_attendants():
    a, b = two_tenants()
    from .helpers import platform_client

    p = platform_client()
    p.patch(f"/api/platform/tenants/{a['tenant'].id}/subscription/", {"plan": "a-plan", "status": "past_due"}, format="json")
    p.patch(f"/api/platform/tenants/{b['tenant'].id}/subscription/", {"plan": "b-plan"}, format="json")
    mine = client_for(a[Role.OWNER]).get("/api/subscription/").json()
    assert mine["plan"] == "a-plan" and mine["status"] == "past_due" and mine["tenant_status"] == "active"
    assert client_for(b[Role.MANAGER]).get("/api/subscription/").json()["plan"] == "b-plan"
    assert client_for(a[Role.ATTENDANT]).get("/api/subscription/").status_code == 403
    owner = client_for(a[Role.OWNER])
    for method in (owner.post, owner.patch, owner.put, owner.delete):
        assert method("/api/subscription/", {"status": "active"}, format="json").status_code == 405
