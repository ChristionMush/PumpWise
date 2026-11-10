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


def test_a_tank_joined_to_another_is_one_stock_and_both_are_dipped_together():
    from apps.operations.models import TankReading

    a, b = two_tenants()
    fc = make_forecourt(a["tenant"])  # Tank 1: 10 000 L, with a dip chart (500 mm = 5 000 L)
    c = platform_client()
    url = f"/api/platform/tenants/{a['tenant'].id}/tanks/{fc.tank.pk}/linked/"
    joined = c.post(url, {"name": "Tank 1B", "capacity_litres": "5000"}, format="json")
    assert joined.status_code == 201, joined.content
    tank = next(t for t in joined.json()["tanks"] if t["name"] == "Tank 1")
    assert tank["capacity_litres"] == "15000.000" and [lt["name"] for lt in tank["linked"]] == ["Tank 1B"]  # one stock, both capacities
    linked_id = tank["linked"][0]["id"]
    assert c.post(url, {"name": "Tank 1B", "capacity_litres": "5000"}, format="json").status_code == 400  # name taken
    assert c.post(url, {"name": "tank 1", "capacity_litres": "5000"}, format="json").status_code == 400  # its own name
    assert c.post(url, {"name": "Tank 1C", "capacity_litres": "0"}, format="json").status_code == 400
    assert client_for(a[Role.OWNER]).post(url, {"name": "Tank 1C", "capacity_litres": "100"}, format="json").status_code == 403

    mgr = client_for(a[Role.MANAGER])
    prefill_tank = mgr.get("/api/shifts/prefill/").json()["stations"][0]["tanks"][0]
    assert prefill_tank["linked"] == [{"id": linked_id, "name": "Tank 1B", "has_calibration": False}]
    alone = mgr.post("/api/tank-readings/", {"tank": str(fc.tank.pk), "dip_mm": "500"}, format="json")
    assert alone.status_code == 400 and "Dip Tank 1B too" in str(alone.json())
    no_chart = mgr.post("/api/tank-readings/", {"tank": str(fc.tank.pk), "dip_mm": "500", "linked": [{"tank": linked_id, "dip_mm": "300"}]}, format="json")
    assert no_chart.status_code == 400 and "no dip chart" in str(no_chart.json())
    both = mgr.post("/api/tank-readings/", {"tank": str(fc.tank.pk), "dip_mm": "500", "linked": [{"tank": linked_id, "dip_mm": "300", "litres": "2000"}]}, format="json")
    assert both.status_code == 201, both.content
    assert both.json()["litres"] == "7000.000"  # 5 000 (Tank 1, from its chart) + 2 000 (Tank 1B, typed)
    with db_context(tenant_id=a["tenant"].id):
        parts = TankReading.objects.get(pk=both.json()["id"]).parts
    assert [(p["name"], p["litres"]) for p in parts] == [("Tank 1", "5000.000"), ("Tank 1B", "2000.000")]

    # the day close takes both too
    meters = [{"nozzle": str(n.pk), "reading": "0"} for n in fc.nozzles]
    closed = mgr.post("/api/day-close/", {"station": str(fc.station.pk), "at": "2026-10-01T19:10:00Z", "meters": meters,
                                          "dips": [{"tank": str(fc.tank.pk), "dip_mm": "1000", "linked": [{"tank": linked_id, "litres": "4500"}]}]}, format="json")
    assert closed.status_code == 201, closed.content
    with db_context(tenant_id=a["tenant"].id):
        assert str(TankReading.objects.filter(day_close__isnull=False).get().litres) == "14500.000"
