"""Phase 3: station setup (tanks, calibration, pumps, nozzles, prices)."""
from datetime import datetime
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.stations.models import Nozzle, Pump
from apps.stations.services import price_at

from .helpers import at, client_for, make_forecourt, two_tenants


def test_owner_builds_a_forecourt_through_the_api():
    a, _ = two_tenants()
    c = client_for(a[Role.OWNER])
    st = c.post("/api/stations/", {"name": "Borrowdale", "pricing_currency": "USD"}, format="json")
    assert st.status_code == 201, st.content
    fuel = c.post("/api/fuel-types/", {"name": "Diesel", "code": "diesel"}, format="json").json()
    assert fuel["code"] == "DIESEL"
    tank = c.post("/api/tanks/", {"station": st.json()["id"], "name": "T1", "fuel_type": fuel["id"], "capacity_litres": "20000", "reserve_litres": "1000"}, format="json")
    assert tank.status_code == 201 and tank.json()["data_source"] == "manual" and tank.json()["qr_token"]
    pump = c.post("/api/pumps/", {"station": st.json()["id"], "tank": tank.json()["id"], "name": "Pump 1", "brand": "Wayne", "model": "Helix", "year": 2018}, format="json")
    assert pump.status_code == 201
    assert pump.json()["data_source"] == "manual" and pump.json()["brand"] == "Wayne" and pump.json()["year"] == 2018
    noz = c.post("/api/nozzles/", {"pump": pump.json()["id"], "number": 1, "label": "Diesel 1"}, format="json")
    assert noz.status_code == 201
    listed = c.get(f"/api/pumps/{pump.json()['id']}/").json()
    assert [n["number"] for n in listed["nozzles"]] == [1]
    assert c.post("/api/nozzles/", {"pump": pump.json()["id"], "number": 1}, format="json").status_code == 400  # duplicate number


def test_pump_hardware_fields_are_optional_and_default_to_manual():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    res = client_for(a[Role.OWNER]).post("/api/pumps/", {"station": str(fc.station.pk), "tank": str(fc.tank.pk), "name": "Bare pump"}, format="json")
    assert res.status_code == 201
    assert res.json()["brand"] == "" and res.json()["year"] is None and res.json()["data_source"] == "manual"


def test_who_may_change_setup():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    body = {"station": str(fc.station.pk), "name": "Tank 2", "fuel_type": str(fc.fuel.pk), "capacity_litres": "5000"}
    assert client_for(a[Role.ATTENDANT]).get("/api/pumps/").status_code == 200  # attendants can look
    assert client_for(a[Role.ATTENDANT]).post("/api/tanks/", body, format="json").status_code == 403
    assert client_for(a[Role.MANAGER]).post("/api/tanks/", body, format="json").status_code == 201
    assert client_for(a[Role.MANAGER]).post("/api/stations/", {"name": "New"}, format="json").status_code == 403  # owner only
    assert client_for(a[Role.OWNER]).post("/api/stations/", {"name": "New"}, format="json").status_code == 201


def test_cannot_link_another_tenants_records_or_mismatched_stations():
    a, b = two_tenants()
    fa, fb = make_forecourt(a["tenant"]), make_forecourt(b["tenant"])
    c = client_for(a[Role.OWNER])
    other_tank = c.post("/api/tanks/", {"station": str(fa.station.pk), "name": "X", "fuel_type": str(fb.fuel.pk), "capacity_litres": "100"}, format="json")
    assert other_tank.status_code == 400  # B's fuel type is invisible to A
    assert c.post("/api/pumps/", {"station": str(fa.station.pk), "tank": str(fb.tank.pk), "name": "P"}, format="json").status_code == 400
    st2 = c.post("/api/stations/", {"name": "Second"}, format="json").json()
    mismatch = c.post("/api/pumps/", {"station": st2["id"], "tank": str(fa.tank.pk), "name": "P"}, format="json")
    assert mismatch.status_code == 400 and "same station" in str(mismatch.json())
    assert c.get(f"/api/tanks/{fb.tank.pk}/").status_code == 404


def test_calibration_chart_and_dip_conversion():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    c = client_for(a[Role.OWNER])
    bad = c.put(f"/api/tanks/{fc.tank.pk}/calibration/", {"points": [{"dip_mm": 0, "litres": 0}, {"dip_mm": 100, "litres": 900}, {"dip_mm": 200, "litres": 500}]}, format="json")
    assert bad.status_code == 400
    assert c.put(f"/api/tanks/{fc.tank.pk}/calibration/", {"points": [{"dip_mm": 0, "litres": 0}]}, format="json").status_code == 400
    ok = c.put(f"/api/tanks/{fc.tank.pk}/calibration/", {"points": [{"dip_mm": 0, "litres": 0}, {"dip_mm": 1000, "litres": 10000}]}, format="json")
    assert ok.status_code == 200
    r = c.post("/api/tank-readings/", {"tank": str(fc.tank.pk), "dip_mm": "250.0"}, format="json")
    assert r.status_code == 201 and Decimal(r.json()["litres"]) == Decimal("2500.000")
    assert c.post("/api/tank-readings/", {"tank": str(fc.tank.pk), "dip_mm": "5000"}, format="json").status_code == 400  # outside chart
    assert c.post("/api/tank-readings/", {"tank": str(fc.tank.pk), "litres": "99999"}, format="json").status_code == 400  # bigger than tank
    assert c.post("/api/tank-readings/", {"tank": str(fc.tank.pk)}, format="json").status_code == 400


def test_dip_without_a_chart_asks_for_litres():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    c = client_for(a[Role.OWNER])
    c.post("/api/tanks/", {"station": str(fc.station.pk), "name": "Tank B", "fuel_type": str(fc.fuel.pk), "capacity_litres": "1000"}, format="json")
    tank_b = [t for t in c.get("/api/tanks/").json()["results"] if t["name"] == "Tank B"][0]
    res = c.post("/api/tank-readings/", {"tank": tank_b["id"], "dip_mm": "100"}, format="json")
    assert res.status_code == 400 and "calibration" in str(res.json()).lower()
    assert c.post("/api/tank-readings/", {"tank": tank_b["id"], "litres": "300"}, format="json").status_code == 201


def test_price_history_never_overlaps_and_sales_use_the_price_in_force():
    a, _ = two_tenants()
    t0 = at(2026, 9, 1)
    fc = make_forecourt(a["tenant"], price="1.50", price_from=t0)
    c = client_for(a[Role.OWNER])
    r = c.post("/api/prices/", {"station": str(fc.station.pk), "fuel_type": str(fc.fuel.pk), "price_per_litre": "1.70", "effective_from": "2026-09-20T08:00:00Z"}, format="json")
    assert r.status_code == 201
    history = c.get(f"/api/prices/?station={fc.station.pk}").json()
    assert len(history) == 2
    old = [h for h in history if h["price_per_litre"].startswith("1.5")][0]
    assert datetime.fromisoformat(old["effective_to"]) == at(2026, 9, 20, 8)  # same instant, shown in local time
    with db_context(tenant_id=a["tenant"].id):
        p = lambda when: price_at(fc.station.pk, fc.fuel.pk, "USD", when).price_per_litre  # noqa: E731
        assert p(at(2026, 9, 10)) == Decimal("1.5000")
        assert p(at(2026, 9, 20, 7, 59)) == Decimal("1.5000")
        assert p(at(2026, 9, 20, 8, 0)) == Decimal("1.7000")  # boundary belongs to the new price
        assert price_at(fc.station.pk, fc.fuel.pk, "USD", at(2026, 8, 1)) is None
    # cannot back-date to before the current price started, cannot set zero
    assert c.post("/api/prices/", {"station": str(fc.station.pk), "fuel_type": str(fc.fuel.pk), "price_per_litre": "1.60", "effective_from": "2026-09-05T00:00:00Z"}, format="json").status_code == 400
    assert c.post("/api/prices/", {"station": str(fc.station.pk), "fuel_type": str(fc.fuel.pk), "price_per_litre": "0"}, format="json").status_code == 400
    # a second currency has its own history
    assert c.post("/api/prices/", {"station": str(fc.station.pk), "fuel_type": str(fc.fuel.pk), "currency": "ZWG", "price_per_litre": "40"}, format="json").status_code == 201
    assert c.post("/api/prices/", {"station": str(fc.station.pk), "fuel_type": str(fc.fuel.pk), "currency": "usd", "price_per_litre": "1"}, format="json").status_code == 400


def test_price_changes_are_audited_and_attendants_cannot_change_prices():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    body = {"station": str(fc.station.pk), "fuel_type": str(fc.fuel.pk), "price_per_litre": "2.00"}
    assert client_for(a[Role.ATTENDANT]).post("/api/prices/", body, format="json").status_code == 403
    assert client_for(a[Role.MANAGER]).post("/api/prices/", body, format="json").status_code == 201
    with db_context(tenant_id=a["tenant"].id):
        e = AuditLog.objects.filter(action="price_set").get()
    assert e.changes["price"] == "2.0000" and e.actor_id == a[Role.MANAGER].id


def test_exchange_rates_are_append_only():
    a, _ = two_tenants()
    c = client_for(a[Role.OWNER])
    r = c.post("/api/exchange-rates/", {"from_currency": "ZWG", "to_currency": "USD", "rate": "0.037"}, format="json")
    assert r.status_code == 201
    assert c.post("/api/exchange-rates/", {"from_currency": "USD", "to_currency": "USD", "rate": "1"}, format="json").status_code == 400
    assert c.patch(f"/api/exchange-rates/{r.json()['id']}/", {"rate": "0.5"}, format="json").status_code == 405
    assert c.delete(f"/api/exchange-rates/{r.json()['id']}/").status_code == 405


def test_delete_is_soft_and_audited():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    res = client_for(a[Role.OWNER]).delete(f"/api/pumps/{fc.pump.pk}/")
    assert res.status_code == 204
    with db_context(tenant_id=a["tenant"].id):
        assert Pump.all_objects.get(pk=fc.pump.pk).deleted_at is not None
        assert not Pump.objects.filter(pk=fc.pump.pk).exists()
        assert Nozzle.all_objects.filter(pump=fc.pump).count() == 2  # nothing physically removed
        assert AuditLog.objects.filter(action="pump_deleted").count() == 1


def test_station_assignments_limit_what_a_manager_sees():
    a, _ = two_tenants()
    f1 = make_forecourt(a["tenant"], name="North")
    f2 = make_forecourt(a["tenant"], name="South", code="DIESEL")
    owner, mgr = client_for(a[Role.OWNER]), client_for(a[Role.MANAGER])
    assert {s["name"] for s in mgr.get("/api/stations/").json()["results"]} == {"North", "South"}  # no assignment = all
    assert owner.put(f"/api/users/{a[Role.MANAGER].id}/stations/", {"station_ids": [str(f1.station.pk)]}, format="json").status_code == 200
    assert {s["name"] for s in mgr.get("/api/stations/").json()["results"]} == {"North"}
    assert mgr.get(f"/api/pumps/{f2.pump.pk}/").status_code == 404
    assert mgr.put(f"/api/users/{a[Role.MANAGER].id}/stations/", {"station_ids": []}, format="json").status_code == 403  # only the owner assigns
    assert owner.put(f"/api/users/{a[Role.MANAGER].id}/stations/", {"station_ids": ["00000000-0000-0000-0000-000000000000"]}, format="json").status_code == 400


def test_pump_photos_are_private_to_their_tenant():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"])
    owner = client_for(a[Role.OWNER])
    png = SimpleUploadedFile("plate.png", b"\x89PNG\r\n\x1a\nfake", content_type="image/png")
    up = owner.post(f"/api/pumps/{fc.pump.pk}/photos/", {"nameplate_photo": png}, format="multipart")
    assert up.status_code == 200 and up.json()["nameplate_photo_url"]
    url = up.json()["nameplate_photo_url"]
    assert owner.get(url).status_code == 200
    assert client_for(b[Role.OWNER]).get(url).status_code == 404  # RLS: B cannot even find the row
    assert APIClient().get(url).status_code == 401
    bad = SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")
    assert owner.post(f"/api/pumps/{fc.pump.pk}/photos/", {"display_photo": bad}, format="multipart").status_code == 400
    assert client_for(a[Role.ATTENDANT]).post(f"/api/pumps/{fc.pump.pk}/photos/", {"display_photo": png}, format="multipart").status_code == 403


def test_qr_codes_resolve_and_render():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"])
    att = client_for(a[Role.ATTENDANT])
    r = att.get(f"/api/qr/{fc.pump.qr_token}/").json()
    assert r["type"] == "pump" and r["id"] == str(fc.pump.pk) and r["station_id"] == str(fc.station.pk)
    assert att.get(f"/api/qr/{fc.tank.qr_token}/").json()["type"] == "tank"
    assert att.get("/api/qr/nonsense/").status_code == 404
    assert client_for(b[Role.ATTENDANT]).get(f"/api/qr/{fc.pump.qr_token}/").status_code == 404  # other tenant's code
    svg = client_for(a[Role.OWNER]).get(f"/api/qr-image/pumps/{fc.pump.pk}/")
    assert svg.status_code == 200 and svg["Content-Type"] == "image/svg+xml" and b"<svg" in svg.content
    assert att.get(f"/api/qr-image/pumps/{fc.pump.pk}/").status_code == 403
