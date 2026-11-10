"""Rule 11: dips, deliveries, tank levels and reconciliation belong to managers and owners only.

Attendants are refused on every such endpoint, even to read, and tank fields never appear in anything they receive.
Each endpoint has its own refusal test plus a positive control, so a refusal is never vacuous.
"""
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import Role
from apps.accounts.permissions import CAPABILITIES, TANK_FIELDS, can
from apps.core.db import db_context
from apps.operations.models import Delivery, TankReading

from .helpers import at, client_for, dip, make_forecourt, make_user, run_shift, two_tenants

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture
def site():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    shift = run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 50], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14))
    reading = dip(a["tenant"], a[Role.MANAGER], fc, 5000, at(2026, 10, 1, 15))
    with db_context(tenant_id=a["tenant"].id):
        from apps.operations import services

        delivery = services.record_delivery(user=a[Role.MANAGER], tank=fc.tank, litres=1000, delivered_at=at(2026, 10, 1, 9))
        reading.photo = SimpleUploadedFile("dip.png", PNG, content_type="image/png")
        reading.save()
        delivery.photo = SimpleUploadedFile("note.png", PNG, content_type="image/png")
        delivery.save()
    return {"a": a, "b": b, "fc": fc, "shift": shift, "dip": reading, "delivery": delivery}


def tank_get_endpoints(s):
    fc = s["fc"]
    return [
        "/api/tank-readings/",
        f"/api/tank-readings/?tank={fc.tank.pk}",
        "/api/deliveries/",
        "/api/tanks/",
        f"/api/tanks/{fc.tank.pk}/",
        f"/api/tanks/{fc.tank.pk}/calibration/",
        f"/api/media/tankreading/{s['dip'].pk}/photo/",
        f"/api/media/delivery/{s['delivery'].pk}/photo/",
        "/api/reconciliation/tanks/",
        "/api/reconciliation/cash/",
        "/api/forecast/",
        "/api/dashboard/",
        "/api/alerts/",
        "/api/reports/daily-sales/",
    ]


GET_INDEXES = range(14)


@pytest.mark.parametrize("i", GET_INDEXES)
def test_attendant_is_refused_every_tank_side_read(site, i):
    url = tank_get_endpoints(site)[i]
    assert client_for(site["a"][Role.ATTENDANT]).get(url).status_code == 403, url


@pytest.mark.parametrize("role", [Role.MANAGER, Role.OWNER])
@pytest.mark.parametrize("i", GET_INDEXES)
def test_managers_and_owners_can_read_every_tank_side_endpoint(site, i, role):
    url = tank_get_endpoints(site)[i]
    assert client_for(site["a"][role]).get(url).status_code == 200, url


def test_attendant_cannot_record_a_dip(site):
    body = {"tank": str(site["fc"].tank.pk), "litres": "4000"}
    assert client_for(site["a"][Role.ATTENDANT]).post("/api/tank-readings/", body, format="json").status_code == 403
    assert client_for(site["a"][Role.MANAGER]).post("/api/tank-readings/", body, format="json").status_code == 201


def test_attendant_cannot_record_a_delivery(site):
    body = {"tank": str(site["fc"].tank.pk), "litres": "2000", "supplier": "Zuva", "note_number": "N-9"}
    assert client_for(site["a"][Role.ATTENDANT]).post("/api/deliveries/", body, format="json").status_code == 403
    assert client_for(site["a"][Role.OWNER]).post("/api/deliveries/", body, format="json").status_code == 201


def test_attendant_cannot_attach_a_photo_to_a_dip_or_delivery(site):
    att, mgr = client_for(site["a"][Role.ATTENDANT]), client_for(site["a"][Role.MANAGER])
    for kind, row in (("dip", site["dip"]), ("delivery", site["delivery"])):
        photo = lambda: SimpleUploadedFile("p.png", PNG, content_type="image/png")  # noqa: E731
        assert att.post(f"/api/readings/{kind}/{row.pk}/photo/", {"photo": photo()}, format="multipart").status_code == 403
        assert mgr.post(f"/api/readings/{kind}/{row.pk}/photo/", {"photo": photo()}, format="multipart").status_code == 200


def test_attendant_cannot_write_tanks_or_calibration(site):
    att, fc = client_for(site["a"][Role.ATTENDANT]), site["fc"]
    body = {"station": str(fc.station.pk), "name": "T9", "fuel_type": str(fc.fuel.pk), "capacity_litres": "100"}
    assert att.post("/api/tanks/", body, format="json").status_code == 403
    assert att.put(f"/api/tanks/{fc.tank.pk}/calibration/", {"points": [{"dip_mm": 0, "litres": 0}, {"dip_mm": 9, "litres": 9}]}, format="json").status_code == 403


def test_attendant_responses_never_carry_tank_fields(site):
    att = client_for(site["a"][Role.ATTENDANT])

    def keys(obj):
        if isinstance(obj, dict):
            return set(obj) | {k for v in obj.values() for k in keys(v)}
        if isinstance(obj, list):
            return {k for v in obj for k in keys(v)}
        return set()

    for url in ("/api/shifts/prefill/", "/api/shifts/", f"/api/shifts/{site['shift'].pk}/", "/api/stations/", "/api/pumps/", "/api/auth/me/"):
        res = att.get(url)
        assert res.status_code == 200, url
        leaked = keys(res.json()) & TANK_FIELDS
        assert not leaked, f"{url} leaked {leaked}"
    # the manager's prefill does include tanks, so the check above is not vacuous
    assert "tanks" in client_for(site["a"][Role.MANAGER]).get("/api/shifts/prefill/").json()["stations"][0]


def test_dips_and_deliveries_record_who_did_it_and_are_idempotent(site):
    mgr, fc = client_for(site["a"][Role.MANAGER]), site["fc"]
    cid = str(uuid.uuid4())
    first = mgr.post("/api/tank-readings/", {"client_id": cid, "tank": str(fc.tank.pk), "litres": "4100"}, format="json")
    again = mgr.post("/api/tank-readings/", {"client_id": cid, "tank": str(fc.tank.pk), "litres": "4100"}, format="json")
    assert first.status_code == 201 and again.status_code == 200 and first.json()["id"] == again.json()["id"]
    assert first.json()["recorded_by"] == str(site["a"][Role.MANAGER].pk)
    did = str(uuid.uuid4())
    body = {"client_id": did, "tank": str(fc.tank.pk), "litres": "900", "supplier": "Zuva", "note_number": "N-2"}
    d1, d2 = mgr.post("/api/deliveries/", body, format="json"), mgr.post("/api/deliveries/", body, format="json")
    assert d1.status_code == 201 and d2.status_code == 200 and d1.json()["id"] == d2.json()["id"]
    with db_context(tenant_id=site["a"]["tenant"].id):
        assert TankReading.objects.filter(client_id=cid).count() == 1 and Delivery.objects.filter(client_id=did).count() == 1


def test_a_client_id_used_by_another_business_never_reveals_or_reuses_its_row(site):
    b, fc = site["b"], site["fc"]
    fb = make_forecourt(b["tenant"])
    cid = str(uuid.uuid4())
    client_for(site["a"][Role.MANAGER]).post("/api/tank-readings/", {"client_id": cid, "tank": str(fc.tank.pk), "litres": "4100"}, format="json")
    res = client_for(b[Role.MANAGER]).post("/api/tank-readings/", {"client_id": cid, "tank": str(fb.tank.pk), "litres": "10"}, format="json")
    assert res.status_code == 201 and res.json()["litres"] == "10.000"


def test_a_manager_is_limited_to_their_own_stations_for_dips(site):
    a, fc = site["a"], site["fc"]
    other = make_forecourt(a["tenant"], name="Second Station", code="DIESEL")
    mgr = make_user(a["tenant"], Role.MANAGER)
    client_for(a[Role.OWNER]).put(f"/api/users/{mgr.pk}/stations/", {"station_ids": [str(fc.station.pk)]}, format="json")
    c = client_for(mgr)
    assert c.post("/api/tank-readings/", {"tank": str(fc.tank.pk), "litres": "100"}, format="json").status_code == 201
    assert c.post("/api/tank-readings/", {"tank": str(other.tank.pk), "litres": "100"}, format="json").status_code == 403


def test_the_policy_table_never_gives_attendants_tank_side_capabilities():
    attendant = type("U", (), {"is_authenticated": True, "role": Role.ATTENDANT})()
    for cap in ("tank_records", "view_tank_levels", "view_reconciliation", "view_dashboard", "view_reports", "manage_shift"):
        assert Role.ATTENDANT not in CAPABILITIES[cap] and not can(attendant, cap)
    assert can(attendant, "record_shift")
