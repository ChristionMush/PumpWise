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


# ---- the owner's exception: an attendant allowed to dip (where the manager is not on site) ----------------------

def pesnat(site, on=True):
    from .helpers import platform_client

    res = platform_client().patch(f"/api/platform/tenants/{site['a']['tenant'].id}/features/", {"pesnat_mode": on}, format="json")
    assert res.status_code == 200 and res.json()["features"]["pesnat_mode"] is on


def allow_dipping(site, on=True):
    pesnat(site)
    att = site["a"][Role.ATTENDANT]
    res = client_for(site["a"][Role.OWNER]).patch(f"/api/users/{att.pk}/", {"can_dip": on}, format="json")
    assert res.status_code == 200 and res.json()["can_dip"] is on
    return client_for(att)


def test_only_the_owner_allows_an_attendant_to_dip_and_only_attendants(site):
    a = site["a"]
    att = a[Role.ATTENDANT]
    assert client_for(a[Role.MANAGER]).patch(f"/api/users/{att.pk}/", {"can_dip": True}, format="json").status_code == 403
    assert client_for(att).patch(f"/api/users/{att.pk}/", {"can_dip": True}, format="json").status_code == 403
    outside = client_for(a[Role.OWNER]).patch(f"/api/users/{att.pk}/", {"can_dip": True}, format="json")
    assert outside.status_code == 400 and "Pesnat mode" in str(outside.json())  # only in Pesnat mode
    pesnat(site)
    refused = client_for(a[Role.OWNER]).patch(f"/api/users/{a[Role.MANAGER].pk}/", {"can_dip": True}, format="json")
    assert refused.status_code == 400 and "attendant" in str(refused.json())
    allow_dipping(site)
    from apps.audit.models import AuditLog

    with db_context(tenant_id=a["tenant"].id):
        assert AuditLog.objects.filter(action="user_updated", changes__can_dip__isnull=False).exists()
    # made a manager, the flag goes (they dip anyway); made an attendant again, they start without it
    owner = client_for(a[Role.OWNER])
    assert owner.patch(f"/api/users/{att.pk}/", {"role": "manager"}, format="json").json()["can_dip"] is False
    assert owner.patch(f"/api/users/{att.pk}/", {"role": "attendant"}, format="json").json()["can_dip"] is False


def test_an_attendant_allowed_to_dip_records_dips_and_nothing_else_on_the_tank_side(site):
    fc = site["fc"]
    body = {"tank": str(fc.tank.pk), "litres": "4000"}
    att = allow_dipping(site)
    made = att.post("/api/tank-readings/", body, format="json")
    assert made.status_code == 201 and made.json()["recorded_by"] == str(site["a"][Role.ATTENDANT].pk)
    from .test_multi_attendant import op

    synced = att.post("/api/sync/", {"ops": [op("tank_dip", {"client_id": str(uuid.uuid4()), "tank": str(fc.tank.pk), "dip_mm": "500"})]}, format="json")
    assert synced.json()["results"][0]["status"] == "applied", synced.content
    photo = lambda: SimpleUploadedFile("p.png", PNG, content_type="image/png")  # noqa: E731
    assert att.post(f"/api/readings/dip/{made.json()['id']}/photo/", {"photo": photo()}, format="multipart").status_code == 200
    assert att.post(f"/api/readings/dip/{site['dip'].pk}/photo/", {"photo": photo()}, format="multipart").status_code == 404  # the manager's dip
    assert att.post(f"/api/readings/delivery/{site['delivery'].pk}/photo/", {"photo": photo()}, format="multipart").status_code == 403
    for url in tank_get_endpoints(site):  # still no levels, history, deliveries or reports
        assert att.get(url).status_code == 403, url
    assert att.post("/api/deliveries/", {**body, "supplier": "Zuva", "note_number": "N-9"}, format="json").status_code == 403
    # the phone learns which tanks to dip, never their levels
    tanks = att.get("/api/shifts/prefill/").json()["stations"][0]["tanks"]
    assert tanks == [{"id": str(fc.tank.pk), "name": fc.tank.name, "fuel_code": fc.fuel.code, "has_calibration": True}]
    assert att.get("/api/auth/me/").json()["user"]["can_dip"] is True
    # taken away again: refused like any attendant
    att = allow_dipping(site, on=False)
    assert att.post("/api/tank-readings/", body, format="json").status_code == 403
    assert "tanks" not in att.get("/api/shifts/prefill/").json()["stations"][0]


def test_turning_pesnat_mode_off_stops_attendants_dipping_and_on_again_restores_it(site):
    body = {"tank": str(site["fc"].tank.pk), "litres": "4000"}
    att = allow_dipping(site)
    assert att.post("/api/tank-readings/", body, format="json").status_code == 201
    pesnat(site, on=False)
    assert att.post("/api/tank-readings/", body, format="json").status_code == 403
    assert "tanks" not in att.get("/api/shifts/prefill/").json()["stations"][0]
    assert att.get("/api/auth/me/").json()["tenant"]["pesnat_mode"] is False
    pesnat(site)  # the owner's choice was kept
    assert att.post("/api/tank-readings/", body, format="json").status_code == 201


def test_in_pesnat_mode_the_dipper_closes_the_day_whenever_they_dip(site):
    """No set 21:00: the day ends when the attendant allowed to dip dips every tank and reads every pump."""
    from apps.operations.models import DayClose

    a, fc = site["a"], site["fc"]
    body = {"station": str(fc.station.pk), "at": "2026-10-02T13:30:00Z",  # 15:30 in Harare: ends 2 Oct's trading day then, not at 21:00
            "dips": [{"tank": str(fc.tank.pk), "dip_mm": "500"}],
            "meters": [{"nozzle": str(n.pk), "reading": v} for n, v in zip(fc.nozzles, ("120", "60"), strict=True)]}
    plain = client_for(a[Role.ATTENDANT])
    assert plain.post("/api/day-close/", body, format="json").status_code == 403  # not allowed to dip: never closes
    att = allow_dipping(site)
    status = att.get("/api/day-close/").json()[0]
    assert status["latest"] is None and status["recent"] == [] and "dips" not in str(status)  # no past dips or levels
    assert [(n["pump"], n["last_reading"]) for n in status["nozzles"]] == [("Pump 1", "100.00"), ("Pump 1", "50.00")]
    made = att.post("/api/day-close/", body, format="json")
    assert made.status_code == 201, made.content
    assert made.json()[0]["closed"]["closed_by"] == a[Role.ATTENDANT].full_name and made.json()[0]["latest"] is None
    with db_context(tenant_id=a["tenant"].id):
        close = DayClose.objects.get()
        assert str(close.day) == "2026-10-02" and close.closed_by_id == a[Role.ATTENDANT].pk
    again = att.post("/api/day-close/", body, format="json")
    assert again.status_code == 400 and "already closed" in str(again.json())
    # correcting a close stays with the manager and the owner
    assert att.patch(f"/api/day-close/{close.pk}/", {"dips": [], "meters": [], "note": ""}, format="json").status_code == 403
    pesnat(site, on=False)
    assert att.get("/api/day-close/").status_code == 403
