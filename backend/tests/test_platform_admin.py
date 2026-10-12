
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.stations.models import Station
from apps.tenants.models import Subscription, Tenant

from .helpers import client_for, create_tenant_via_api, make_tenant, make_user, two_tenants

PLATFORM_ROUTES = [
    ("get", "/api/platform/tenants/"),
    ("post", "/api/platform/tenants/"),
    ("get", "/api/platform/tenants/{id}/"),
    ("patch", "/api/platform/tenants/{id}/"),
    ("get", "/api/platform/tenants/{id}/stations/"),
    ("post", "/api/platform/tenants/{id}/stations/"),
    ("get", "/api/platform/tenants/{id}/subscription/"),
    ("patch", "/api/platform/tenants/{id}/subscription/"),
    ("post", "/api/platform/tenants/{id}/reset-owner-password/"),
    ("get", "/api/platform/audit/"),
]


def _login(identifier, password):
    return APIClient().post("/api/auth/login/", {"identifier": identifier, "password": password}, format="json")


def _auth(access):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return c


def _count(model, **ctx):
    with db_context(**ctx):
        return (getattr(model, "all_objects", None) or model.objects).count()


@pytest.mark.parametrize("method,url", PLATFORM_ROUTES)
def test_every_platform_route_is_closed_to_everyone_but_platform_admins(method, url):
    a, _ = two_tenants()
    url = url.format(id=a["tenant"].id)
    assert getattr(APIClient(), method)(url, {}, format="json").status_code == 401
    for role in (Role.OWNER, Role.MANAGER, Role.ATTENDANT):
        res = getattr(client_for(a[role]), method)(url, {}, format="json")
        assert res.status_code == 403, f"{role} {method} {url}"


def test_create_tenant_builds_everything_and_the_owner_can_get_started():
    client, res = create_tenant_via_api()
    assert res.status_code == 201, res.content
    body = res.json()
    assert res["Cache-Control"] == "no-store"
    assert body["name"] == "Msasa Fuel Services" and body["slug"] == "msasa-fuel-services"
    assert body["subscription"] == {"status": "active", "plan": "standard", "paid_through": None}
    assert body["station_count"] == 2 and {s["name"] for s in body["stations"]} == {"Msasa Road", "Borrowdale"}
    assert body["owner"]["phone"] == "+263772123456"
    creds = body["owner_credentials"]
    assert creds["login"] == "+263772123456" and len(creds["temporary_password"]) == 10

    # the owner logs in with the temporary password and MUST change it
    login = _login("0772123456", creds["temporary_password"])
    assert login.status_code == 200 and login.json()["user"]["must_change_password"] is True
    owner = _auth(login.json()["access"])
    me = owner.get("/api/auth/me/")
    assert me.status_code == 200 and me.json()["tenant"]["name"] == "Msasa Fuel Services"
    blocked = owner.get("/api/stations/")
    assert blocked.status_code == 403 and blocked.json()["code"] == "password_change_required"

    changed = owner.post("/api/auth/change-password/", {"current_password": creds["temporary_password"], "new_password": "My-new-pass-77"}, format="json")
    assert changed.status_code == 200
    owner = _auth(changed.json()["access"])
    stations = owner.get("/api/stations/").json()["results"]
    assert {s["name"] for s in stations} == {"Msasa Road", "Borrowdale"}
    assert owner.get("/api/users/").status_code == 200


def test_temporary_password_is_never_stored_or_logged_in_clear():
    client, res = create_tenant_via_api()
    secret = res.json()["owner_credentials"]["temporary_password"]
    tid = res.json()["id"]
    with db_context(tenant_id=tid, platform_admin=True):
        assert secret not in str(list(AuditLog.objects.values_list("changes", flat=True)))
        assert not User.all_objects.filter(password=secret).exists()
    assert secret not in client.get(f"/api/platform/tenants/{tid}/").content.decode()
    assert secret not in client.get("/api/platform/tenants/").content.decode()


def test_creation_is_atomic_duplicate_owner_leaves_nothing_behind():
    other = make_tenant("Existing Co")
    make_user(other, Role.OWNER, email="taken@example.com", phone="+263771111111")
    client = None
    for field, value in (("owner_email", "Taken@Example.com"), ("owner_phone", "0771111111")):
        before = (_count(Tenant, platform_admin=True), _count(Subscription, platform_admin=True))
        client, res = create_tenant_via_api(client, **{field: value, **({"owner_phone": None} if field == "owner_email" else {})})
        assert res.status_code == 400, field
        assert "already in use" in res.json()["detail"]
        assert "Existing" not in res.content.decode()
        assert (_count(Tenant, platform_admin=True), _count(Subscription, platform_admin=True)) == before
    with db_context(tenant_id=other.id):
        assert Station.all_objects.count() == 0


def test_create_validation():
    client, res = create_tenant_via_api(owner_phone=None)  # no email either
    assert res.status_code == 400
    assert create_tenant_via_api(client, owner_phone="abc")[1].status_code == 400
    assert create_tenant_via_api(client, business_name=" ")[1].status_code == 400
    dup = create_tenant_via_api(client, stations=[{"name": "Same"}, {"name": "same"}])[1]
    assert dup.status_code == 400
    too_many = create_tenant_via_api(client, stations=[{"name": f"S{i}"} for i in range(21)])[1]
    assert too_many.status_code == 400
    assert _count(Tenant, platform_admin=True) == 0


def test_same_business_name_twice_gets_distinct_slugs():
    client, first = create_tenant_via_api()
    _, second = create_tenant_via_api(client, owner_phone="0773000000")
    assert first.status_code == second.status_code == 201
    assert first.json()["slug"] != second.json()["slug"]


def test_list_shows_subscription_owner_and_station_count_with_filters():
    client, a = create_tenant_via_api()
    _, b = create_tenant_via_api(client, business_name="Zambezi Petroleum", owner_phone="0773111111", stations=[])
    client.patch(f"/api/platform/tenants/{b.json()['id']}/", {"status": "suspended"}, format="json")
    client.patch(f"/api/platform/tenants/{b.json()['id']}/subscription/", {"status": "past_due"}, format="json")
    rows = client.get("/api/platform/tenants/").json()["results"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["Msasa Fuel Services"]["station_count"] == 2
    assert by_name["Zambezi Petroleum"]["station_count"] == 0
    assert by_name["Zambezi Petroleum"]["subscription"]["status"] == "past_due"
    assert by_name["Zambezi Petroleum"]["owner"]["full_name"] == "Rudo Chikwanha"
    assert [r["name"] for r in client.get("/api/platform/tenants/?status=suspended").json()["results"]] == ["Zambezi Petroleum"]
    assert [r["name"] for r in client.get("/api/platform/tenants/?subscription=active").json()["results"]] == ["Msasa Fuel Services"]
    assert [r["name"] for r in client.get("/api/platform/tenants/?q=zamb").json()["results"]] == ["Zambezi Petroleum"]
    page = client.get("/api/platform/tenants/?limit=1").json()
    assert page["count"] == 2 and len(page["results"]) == 1 and page["next"]


def test_tenant_detail_and_unknown_ids():
    client, res = create_tenant_via_api()
    tid = res.json()["id"]
    detail = client.get(f"/api/platform/tenants/{tid}/").json()
    assert detail["owner"]["full_name"] == "Rudo Chikwanha" and len(detail["stations"]) == 2
    for url in ("/api/platform/tenants/00000000-0000-0000-0000-000000000000/", "/api/platform/tenants/not-a-uuid/"):
        assert client.get(url).status_code == 404


def test_rename_and_status_changes_are_audited():
    client, res = create_tenant_via_api()
    tid = res.json()["id"]
    r = client.patch(f"/api/platform/tenants/{tid}/", {"name": "Msasa Fuel Group", "status": "suspended"}, format="json")
    assert r.status_code == 200 and r.json()["name"] == "Msasa Fuel Group" and r.json()["status"] == "suspended"
    assert client.patch(f"/api/platform/tenants/{tid}/", {"status": "banana"}, format="json").status_code == 400
    with db_context(tenant_id=tid):
        entry = AuditLog.objects.filter(action="tenant_updated").get()
    assert entry.via_platform and entry.changes == {"name": ["Msasa Fuel Services", "Msasa Fuel Group"], "status": ["active", "suspended"]}


def test_subscription_edit_validates_and_audits():
    client, res = create_tenant_via_api()
    tid = res.json()["id"]
    url = f"/api/platform/tenants/{tid}/subscription/"
    ok = client.patch(url, {"status": "past_due", "paid_through": "2026-12-31", "plan": "premium"}, format="json")
    assert ok.status_code == 200 and ok.json() == {"status": "past_due", "plan": "premium", "paid_through": "2026-12-31"}
    assert client.patch(url, {"status": "nope"}, format="json").status_code == 400
    assert client.patch(url, {"paid_through": "tomorrow"}, format="json").status_code == 400
    assert client.get(url).json()["status"] == "past_due"
    with db_context(tenant_id=tid):
        assert AuditLog.objects.filter(action="subscription_updated").count() == 1


def test_platform_can_add_stations_and_duplicates_are_refused():
    client, res = create_tenant_via_api()
    tid = res.json()["id"]
    url = f"/api/platform/tenants/{tid}/stations/"
    assert client.post(url, {"name": "Chitungwiza", "address": "Seke Rd"}, format="json").status_code == 201
    assert client.post(url, {"name": "chitungwiza"}, format="json").status_code == 400
    assert client.post(url, {"name": ""}, format="json").status_code == 400
    assert len(client.get(url).json()) == 3
    assert client.get(f"/api/platform/tenants/{tid}/").json()["station_count"] == 3


def test_reset_owner_password_replaces_credentials_and_signs_owner_out():
    client, res = create_tenant_via_api()
    tid, first = res.json()["id"], res.json()["owner_credentials"]["temporary_password"]
    old_access = _login("0772123456", first).json()["access"]
    reset = client.post(f"/api/platform/tenants/{tid}/reset-owner-password/")
    assert reset.status_code == 200 and reset["Cache-Control"] == "no-store"
    new = reset.json()["owner_credentials"]["temporary_password"]
    assert new != first
    assert _auth(old_access).get("/api/auth/me/").status_code == 401
    assert _login("0772123456", first).status_code == 401
    relogin = _login("0772123456", new)
    assert relogin.status_code == 200 and relogin.json()["user"]["must_change_password"] is True
    assert client.post(f"/api/platform/tenants/{tid}/reset-owner-password/", {"owner_id": "00000000-0000-0000-0000-000000000000"}, format="json").status_code == 404


def test_platform_actions_are_audited_visible_to_tenant_as_the_company_and_to_platform():
    client, a = create_tenant_via_api()
    _, b = create_tenant_via_api(client, business_name="Other Co", owner_phone="0773222222")
    a_pw = a.json()["owner_credentials"]["temporary_password"]
    owner_a = _auth(_login("0772123456", a_pw).json()["access"])
    owner_a.post("/api/auth/change-password/", {"current_password": a_pw, "new_password": "My-new-pass-77"}, format="json")
    owner_a = _auth(_login("0772123456", "My-new-pass-77").json()["access"])

    mine = owner_a.get("/api/audit/").json()["results"]
    created = [e for e in mine if e["action"] == "tenant_created"]
    assert len(created) == 1 and created[0]["via_platform"] and created[0]["actor_name"] == "Tendeka & Elichi"
    assert "Other Co" not in str(mine)  # tenant A never sees tenant B's trail

    platform = client.get("/api/platform/audit/").json()["results"]
    assert {e["tenant_name"] for e in platform if e["action"] == "tenant_created"} == {"Msasa Fuel Services", "Other Co"}
    assert all(e["actor_name"] for e in platform if e["action"] == "tenant_created")


def test_platform_context_alone_never_exposes_tenant_business_data():
    client, _ = create_tenant_via_api()
    with db_context(platform_admin=True):
        assert Station.all_objects.count() == 0  # no tenant narrowed => no stations
        assert User.all_objects.count() == 1  # only the platform admin
        assert Tenant.all_objects.count() == 1


def test_a_tenant_cannot_forge_a_platform_audit_entry():
    from django.db import DatabaseError, transaction

    a, _ = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        with pytest.raises(DatabaseError), transaction.atomic():
            AuditLog.objects.create(tenant_id=a["tenant"].id, action="fake", via_platform=True)
        AuditLog.objects.create(tenant_id=a["tenant"].id, action="legit", via_platform=False)
