"""The platform adds tanks to a business's station and sets which tank each pump nozzle draws from (any business)."""
from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.stations.models import FuelType

from .helpers import client_for, make_forecourt, platform_client, two_tenants
from .test_multi_attendant import readings


def test_the_platform_adds_a_tank_and_points_a_nozzle_at_it():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"])
    c = platform_client()
    url = f"/api/platform/tenants/{a['tenant'].id}/stations/{fc.station.pk}/tanks/"
    listed = c.get(url).json()
    assert [t["name"] for t in listed["tanks"]] == ["Tank 1"] and {n["tank"] for n in listed["nozzles"]} == {str(fc.tank.pk)}
    assert [f["code"] for f in listed["fuels"]] == ["PETROL"]

    body = {"name": "Tank 2", "fuel_type": str(fc.fuel.pk), "capacity_litres": "20000", "reserve_litres": "1000"}
    made = c.post(url, body, format="json")
    assert made.status_code == 201, made.content
    tank2 = next(t for t in made.json()["tanks"] if t["name"] == "Tank 2")
    assert tank2["capacity_litres"] == "20000.000" and tank2["fuel_code"] == "PETROL" and tank2["has_calibration"] is False
    assert c.post(url, body, format="json").status_code == 400  # the name is taken at this station
    assert c.post(url, {**body, "name": "Tank 3", "reserve_litres": "30000"}, format="json").status_code == 400  # reserve above capacity
    with db_context(tenant_id=b["tenant"].id):
        other_fuel = FuelType.objects.create(tenant_id=b["tenant"].id, name="Diesel", code="DIESEL")
    assert c.post(url, {**body, "name": "Tank 4", "fuel_type": str(other_fuel.pk)}, format="json").status_code == 400  # not this business's fuel

    nozzle = str(fc.nozzles[1].pk)
    moved = c.patch(f"/api/platform/tenants/{a['tenant'].id}/nozzles/{nozzle}/tank/", {"tank": tank2["id"]}, format="json")
    assert moved.status_code == 200, moved.content
    assert next(n for n in moved.json()["nozzles"] if n["id"] == nozzle)["tank"] == tank2["id"]
    with db_context(tenant_id=a["tenant"].id):
        assert set(AuditLog.objects.filter(via_platform=True).values_list("action", flat=True)) >= {"tank_created", "nozzle_tank_set"}

    # only the platform, only this business's stations, and never while a shift is open
    assert client_for(a[Role.OWNER]).post(url, {**body, "name": "Tank 5"}, format="json").status_code == 403
    fb = make_forecourt(b["tenant"])
    assert c.get(f"/api/platform/tenants/{a['tenant'].id}/stations/{fb.station.pk}/tanks/").status_code == 404
    client_for(a[Role.ATTENDANT]).post("/api/shifts/start/", {"station": str(fc.station.pk), "readings": readings(fc.nozzles, [0, 0])}, format="json")
    busy = c.patch(f"/api/platform/tenants/{a['tenant'].id}/nozzles/{nozzle}/tank/", {"tank": str(fc.tank.pk)}, format="json")
    assert busy.status_code == 400 and "shift is open" in str(busy.json())


def test_moving_a_nozzle_to_another_fuels_tank_drops_its_links_to_the_old_fuel():
    from apps.stations.models import NozzleTank
    from apps.stations.pools import pools_for

    from .helpers import second_tank

    a, b = two_tenants()
    fc = make_forecourt(a["tenant"])
    petrol2 = second_tank(fc, code="PETROL", name="Tank 2")
    diesel = second_tank(fc, code="DIESEL", name="Diesel Tank")
    nozzle = fc.nozzles[1]
    assert client_for(a[Role.OWNER]).patch(f"/api/nozzles/{nozzle.pk}/", {"also_draws_from": [str(petrol2.pk)]}, format="json").status_code == 200
    moved = platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/nozzles/{nozzle.pk}/tank/", {"tank": str(diesel.pk)}, format="json")
    assert moved.status_code == 200, moved.content
    assert next(n for n in moved.json()["nozzles"] if n["id"] == str(nozzle.pk))["draws_from"] == ["Diesel Tank"]
    with db_context(tenant_id=a["tenant"].id):
        assert not NozzleTank.objects.filter(nozzle=nozzle).exists()
        names = [p.name for p in pools_for(list(type(fc.tank).objects.select_related("fuel_type", "station").order_by("name")))]
    assert names == ["Diesel Tank", "Tank 1", "Tank 2"]  # petrol and diesel never one stock
