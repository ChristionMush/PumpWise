from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.core.db import db_context

from .helpers import (
    PASSWORD,
    client_for,
    make_platform_admin,
    make_tenant,
    make_user,
    two_tenants,
)

NEW = {"full_name": "Tendai Moyo", "phone": "0772000111", "role": "attendant", "password": PASSWORD}


def test_attendant_cannot_reach_owner_or_manager_endpoints():
    a, _ = two_tenants()
    c = client_for(a[Role.ATTENDANT])
    assert c.get("/api/users/").status_code == 403
    assert c.post("/api/users/", NEW, format="json").status_code == 403
    assert c.get("/api/audit/").status_code == 403
    assert c.get("/api/platform/tenants/").status_code == 403


def test_manager_can_read_users_but_not_change_them_or_see_audit():
    a, _ = two_tenants()
    c = client_for(a[Role.MANAGER])
    assert c.get("/api/users/").status_code == 200
    assert c.post("/api/users/", NEW, format="json").status_code == 403
    assert c.patch(f"/api/users/{a[Role.ATTENDANT].id}/", {"full_name": "x"}, format="json").status_code == 403
    assert c.delete(f"/api/users/{a[Role.ATTENDANT].id}/").status_code == 403
    assert c.get("/api/audit/").status_code == 403
    assert c.get("/api/platform/tenants/").status_code == 403


def test_owner_cannot_reach_platform_endpoints():
    a, _ = two_tenants()
    assert client_for(a[Role.OWNER]).get("/api/platform/tenants/").status_code == 403


def test_owner_creates_user_with_normalised_phone_and_can_log_in_as_them():
    a, _ = two_tenants()
    res = client_for(a[Role.OWNER]).post("/api/users/", NEW, format="json")
    assert res.status_code == 201 and res.json()["phone"] == "+263772000111"
    from rest_framework.test import APIClient

    assert APIClient().post("/api/auth/login/", {"identifier": "0772000111", "password": PASSWORD}, format="json").status_code == 200


def test_owner_cannot_create_owners_or_platform_admins():
    a, _ = two_tenants()
    c = client_for(a[Role.OWNER])
    for role in ("owner", "platform_admin", "superuser"):
        res = c.post("/api/users/", {**NEW, "role": role}, format="json")
        assert res.status_code == 400, role


def test_user_needs_email_or_phone():
    a, _ = two_tenants()
    res = client_for(a[Role.OWNER]).post("/api/users/", {"full_name": "No Contact", "role": "manager", "password": PASSWORD}, format="json")
    assert res.status_code == 400


def test_owner_cannot_touch_other_owners_or_change_own_role_or_delete_self():
    t = make_tenant()
    o1, o2 = make_user(t, Role.OWNER), make_user(t, Role.OWNER)
    c = client_for(o1)
    assert c.patch(f"/api/users/{o2.id}/", {"full_name": "x"}, format="json").status_code == 403
    assert c.delete(f"/api/users/{o2.id}/").status_code == 403
    assert c.patch(f"/api/users/{o1.id}/", {"role": "manager"}, format="json").status_code == 400
    assert c.patch(f"/api/users/{o1.id}/", {"is_active": False}, format="json").status_code == 400
    assert c.delete(f"/api/users/{o1.id}/").status_code == 400
    assert c.patch(f"/api/users/{o1.id}/", {"full_name": "New Name"}, format="json").status_code == 200


def test_delete_is_soft_and_keeps_history():
    a, _ = two_tenants()
    victim = a[Role.ATTENDANT]
    assert client_for(a[Role.OWNER]).delete(f"/api/users/{victim.id}/").status_code == 204
    with db_context(tenant_id=a["tenant"].id):
        row = User.all_objects.get(pk=victim.pk)
        assert row.deleted_at is not None and row.is_active is False
        assert not User.objects.filter(pk=victim.pk).exists()  # hidden from the default manager
    assert client_for(a[Role.OWNER]).get("/api/users/").json()["count"] == 2


def test_password_reset_by_owner_signs_the_user_out():
    a, _ = two_tenants()
    att = a[Role.ATTENDANT]
    att_client = client_for(att)
    owner = client_for(a[Role.OWNER])
    res = owner.patch(f"/api/users/{att.id}/", {"password": "Fresh-pass-123"}, format="json")
    assert res.status_code == 200 and res.json()["must_change_password"] is True  # they choose their own at next sign-in
    assert att_client.get("/api/auth/me/").status_code == 401
    # the owner's own password changes only with their current one (Your account, Change my password)
    mine = owner.patch(f"/api/users/{a[Role.OWNER].id}/", {"password": "Another-pass-456"}, format="json")
    assert mine.status_code == 400 and "current password" in str(mine.json())
    changed = owner.post("/api/auth/change-password/", {"current_password": PASSWORD, "new_password": "Another-pass-456"}, format="json")
    assert changed.status_code == 200, changed.content


def test_audit_records_who_changed_what_without_secrets():
    a, _ = two_tenants()
    owner, att = a[Role.OWNER], a[Role.ATTENDANT]
    c = client_for(owner)
    created = c.post("/api/users/", NEW, format="json").json()
    c.patch(f"/api/users/{att.id}/", {"full_name": "Renamed", "password": "Fresh-pass-123"}, format="json")
    c.delete(f"/api/users/{created['id']}/")
    with db_context(tenant_id=a["tenant"].id):
        logs = list(AuditLog.objects.order_by("created_at"))
    actions = [e.action for e in logs]
    assert actions == ["user_created", "user_updated", "password_reset", "user_deleted"]
    assert all(e.actor_id == owner.id for e in logs)
    update = logs[1]
    assert update.object_id == str(att.id) and update.changes["full_name"][1] == "Renamed"
    assert "password" not in str(logs[0].changes) and "password" not in str(update.changes)
    assert all(e.ip_address for e in logs)


def test_platform_admin_is_not_a_tenant_user():
    admin = make_platform_admin()
    assert admin.tenant_id is None and admin.role == Role.PLATFORM_ADMIN
    body = client_for(admin).get("/api/auth/me/").json()
    assert body["tenant"] is None
