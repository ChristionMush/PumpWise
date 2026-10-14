"""The pump system the platform chooses when it creates a business: shared pumps, or assigned pumps."""
from apps.accounts.models import Role, User
from apps.core.db import db_context

from .helpers import client_for, create_tenant_via_api


def owner_of(tenant_id):
    with db_context(tenant_id=tenant_id):
        owner = User.objects.get(role=Role.OWNER)
        User.objects.filter(pk=owner.pk).update(must_change_password=False)  # past the first-sign-in password change
        owner.refresh_from_db()
        return client_for(owner)


def test_a_business_created_with_assigned_pumps_gets_them_on_every_station():
    c, res = create_tenant_via_api(pump_system="assigned")
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["pump_system"] == "assigned"
    assert body["features"] == {"multi_attendant_enabled": True, "pump_assignment_enabled": True}
    assert {(s["attendants_mode"], s["assignment_mode"]) for s in body["stations"]} == {("multiple", "assigned")}
    # a station the owner adds later follows the same system
    new = owner_of(body["id"]).post("/api/stations/", {"name": "Avondale"}, format="json")
    assert new.status_code == 201 and (new.json()["attendants_mode"], new.json()["assignment_mode"]) == ("multiple", "assigned")
    # and so does one the platform adds
    added = c.post(f"/api/platform/tenants/{body['id']}/stations/", {"name": "Mabelreign"}, format="json")
    assert added.status_code == 201 and added.json()["assignment_mode"] == "assigned"


def test_shared_pumps_is_the_default_and_owners_cannot_change_the_system():
    c, res = create_tenant_via_api()
    body = res.json()
    assert body["pump_system"] == "shared"
    assert {(s["attendants_mode"], s["assignment_mode"]) for s in body["stations"]} == {("single", "shared")}
    owner = owner_of(body["id"])
    assert owner.patch(f"/api/platform/tenants/{body['id']}/pump-system/", {"pump_system": "assigned"}, format="json").status_code == 403
    assert owner.post("/api/stations/", {"name": "Avondale"}, format="json").json()["assignment_mode"] == "shared"
    assert create_tenant_via_api(c, business_name="Other Fuel", owner_phone="0772999888", pump_system="both")[1].status_code == 400


def test_the_platform_can_switch_the_system_later():
    c, res = create_tenant_via_api()
    tid = res.json()["id"]
    switched = c.patch(f"/api/platform/tenants/{tid}/pump-system/", {"pump_system": "assigned"}, format="json")
    assert switched.status_code == 200 and switched.json()["pump_system"] == "assigned"
    assert {s["assignment_mode"] for s in switched.json()["stations"]} == {"assigned"}
    back = c.patch(f"/api/platform/tenants/{tid}/pump-system/", {"pump_system": "shared"}, format="json").json()
    assert back["pump_system"] == "shared" and {s["assignment_mode"] for s in back["stations"]} == {"shared"}
