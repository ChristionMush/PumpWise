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

from .helpers import at, client_for, make_forecourt, make_user, second_tank, two_tenants


def test_owner_builds_a_forecourt_through_the_api():
    a, _ = two_tenants()
    c = client_for(a[Role.OWNER])
    st = c.post("/api/stations/", {"name": "Borrowdale", "pricing_currency": "USD"}, format="json")
    assert st.status_code == 201, st.content
    fuel = c.post("/api/fuel-types/", {"name": "Diesel", "code": "diesel"}, format="json").json()
    assert fuel["code"] == "DIESEL"
    tank = c.post("/api/tanks/", {"station": st.json()["id"], "name": "T1", "fuel_type": fuel["id"], "capacity_litres": "20000", "reserve_litres": "1000"}, format="json")
    assert tank.status_code == 201 and tank.json()["data_source"] == "manual"
    petrol = c.post("/api/fuel-types/", {"name": "Petrol", "code": "petrol"}, format="json").json()
    tank2 = c.post("/api/tanks/", {"station": st.json()["id"], "name": "T2", "fuel_type": petrol["id"], "capacity_litres": "20000"}, format="json").json()
    body = {"station": st.json()["id"], "name": "Pump 1", "brand": "Wayne", "model": "Helix", "year": 2018}
    # a pump starts with one nozzle; more can be added, on any tank at the station
    none = c.post("/api/pumps/", {**body, "nozzles": []}, format="json")
    assert none.status_code == 400 and "at least one nozzle" in str(none.json())
    solo = c.post("/api/pumps/", {**body, "name": "Solo", "nozzles": [{"tank": tank.json()["id"]}]}, format="json")
    assert solo.status_code == 201 and len(solo.json()["nozzles"]) == 1
    assert c.delete(f"/api/nozzles/{solo.json()['nozzles'][0]['id']}/").status_code == 400  # its only nozzle stays
    pump = c.post("/api/pumps/", {**body, "nozzles": [{"tank": tank.json()["id"], "label": "Diesel"}, {"tank": tank2["id"]}]}, format="json")
    assert pump.status_code == 201, pump.content
    assert pump.json()["data_source"] == "manual" and pump.json()["brand"] == "Wayne" and pump.json()["year"] == 2018
    listed = c.get(f"/api/pumps/{pump.json()['id']}/").json()
    assert [(n["number"], n["fuel_code"], n["label"]) for n in listed["nozzles"]] == [(1, "DIESEL", "Diesel"), (2, "PETROL", "Petrol")]
    nozzle1, nozzle2 = listed["nozzles"]
    # some pumps have more nozzles: add a third (it may share a tank)
    third = c.post("/api/nozzles/", {"pump": pump.json()["id"], "tank": tank2["id"]}, format="json")
    assert third.status_code == 201 and third.json()["number"] == 3 and third.json()["label"] == "Petrol"
    big = c.post("/api/pumps/", {**body, "name": "Pump 2", "nozzles": [{"tank": tank.json()["id"]}, {"tank": tank2["id"]}, {"tank": tank2["id"]}, {"tank": tank.json()["id"]}]}, format="json")
    assert big.status_code == 201 and len(big.json()["nozzles"]) == 4
    # nozzles can be moved to another tank and removed, as long as the pump keeps one
    assert c.patch(f"/api/nozzles/{nozzle1['id']}/", {"tank": tank2["id"]}, format="json").status_code == 200  # all on petrol now
    assert c.delete(f"/api/nozzles/{third.json()['id']}/").status_code == 204
    assert c.delete(f"/api/nozzles/{nozzle1['id']}/").status_code == 204
    assert c.delete(f"/api/nozzles/{nozzle2['id']}/").status_code == 400  # the last one stays
    assert c.patch(f"/api/nozzles/{nozzle2['id']}/", {"label": "Unleaded"}, format="json").status_code == 200


def test_pump_hardware_fields_are_optional_and_default_to_manual():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    other = second_tank(fc)
    nozzles = [{"tank": str(fc.tank.pk)}, {"tank": str(other.pk)}]
    res = client_for(a[Role.OWNER]).post("/api/pumps/", {"station": str(fc.station.pk), "name": "Bare pump", "nozzles": nozzles}, format="json")
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
    mine = second_tank(fa)
    theirs = {"station": str(fa.station.pk), "name": "P", "nozzles": [{"tank": str(fa.tank.pk)}, {"tank": str(fb.tank.pk)}]}
    assert c.post("/api/pumps/", theirs, format="json").status_code == 400  # B's tank is invisible to A
    st2 = c.post("/api/stations/", {"name": "Second"}, format="json").json()
    mismatch = c.post("/api/pumps/", {"station": st2["id"], "name": "P", "nozzles": [{"tank": str(fa.tank.pk)}, {"tank": str(mine.pk)}]}, format="json")
    assert mismatch.status_code == 400 and "pump's station" in str(mismatch.json())
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


def test_qr_codes_are_switched_off():
    """Pumps and tanks are labelled physically for now: no QR endpoints, and no QR tokens in what the app receives."""
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    owner = client_for(a[Role.OWNER])
    assert owner.get(f"/api/qr/{fc.pump.qr_token}/").status_code == 404
    assert owner.get(f"/api/qr-image/pumps/{fc.pump.pk}/").status_code == 404
    assert "qr_token" not in owner.get(f"/api/pumps/{fc.pump.pk}/").json()
    assert "qr_token" not in owner.get(f"/api/tanks/{fc.tank.pk}/").json()


def test_one_manager_per_station_and_only_the_owner_adds_stations():
    a, _ = two_tenants()
    north, south = make_forecourt(a["tenant"], name="North"), make_forecourt(a["tenant"], name="South", code="DIESEL")
    second = make_user(a["tenant"], Role.MANAGER, full_name="Second Manager")
    owner, mgr = client_for(a[Role.OWNER]), client_for(a[Role.MANAGER])
    assert mgr.post("/api/stations/", {"name": "Mine"}, format="json").status_code == 403  # stations are added by the owner
    url = f"/api/users/{a[Role.MANAGER].id}/stations/"
    both = owner.put(url, {"station_ids": [str(north.station.pk), str(south.station.pk)]}, format="json")
    assert both.status_code == 400 and "one station" in str(both.json())
    assert owner.put(url, {"station_ids": [str(north.station.pk)]}, format="json").status_code == 200
    taken = owner.put(f"/api/users/{second.id}/stations/", {"station_ids": [str(north.station.pk)]}, format="json")
    assert taken.status_code == 400 and "already manages North" in str(taken.json())
    assert owner.put(f"/api/users/{second.id}/stations/", {"station_ids": [str(south.station.pk)]}, format="json").status_code == 200
    # attendants may still work at several stations
    att = f"/api/users/{a[Role.ATTENDANT].id}/stations/"
    assert owner.put(att, {"station_ids": [str(north.station.pk), str(south.station.pk)]}, format="json").status_code == 200
    team = {u["full_name"]: u["station_ids"] for u in owner.get("/api/users/").json()["results"]}
    assert team["Second Manager"] == [str(south.station.pk)]
