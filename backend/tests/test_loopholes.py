"""Checks added after the Nov 2026 mix-ups at Usheokunze and Eastview: numbers that cannot be right are refused or
flagged where someone can act on them, flagged ends need a reason to approve, the manager records receiving the cash,
and alerts catch meter numbers out of order, a handover the pump does not show, and deliveries the dips disagree with."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.insights.alerts import evaluate_tenant
from apps.insights.models import Alert
from apps.operations import services
from apps.operations.models import PumpMeterReading

from .helpers import client_for, dip, make_forecourt, run_shift, two_tenants
from .test_multi_attendant import assigned_site, cash, readings


def body(fc, values, **extra):
    return {"station": str(fc.station.pk), "readings": readings(fc.nozzles, values), **extra}


def stage(client):
    return client.get("/api/shifts/prefill/").json()["stations"][0]["my_stage"]


def open_alerts(tenant, kind):
    with db_context(tenant_id=tenant.id):
        evaluate_tenant(tenant.id)
        return {a.dedupe_key: a.message for a in Alert.objects.filter(kind=kind, status="open")}


# ---- numbers that cannot be right ---------------------------------------------------------------------------------

def test_an_end_below_the_start_or_beyond_what_a_pump_can_pump_is_refused_with_signal():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att = client_for(a[Role.ATTENDANT])
    sid = att.post("/api/shifts/start/", body(fc, [1000, 5000]), format="json").json()["id"]
    below = att.post(f"/api/shifts/{sid}/end/", {**body(fc, [900, 5040]), "cash": cash("60")}, format="json")
    assert below.status_code == 400 and "below the number it started from, 1000.00" in str(below.json())
    assert "ask your manager to correct it" in str(below.json())
    too_much = att.post(f"/api/shifts/{sid}/end/", {**body(fc, [9000, 5040]), "cash": cash("60")}, format="json")
    assert too_much.status_code == 400 and "more than a pump can pump" in str(too_much.json())
    assert att.post(f"/api/shifts/{sid}/end/", {**body(fc, [1100, 5040]), "cash": cash("210")}, format="json").status_code == 200


def test_a_sent_back_fix_below_the_start_is_refused_for_the_attendant():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    sid = att.post("/api/shifts/start/", body(fc, [1000, 5000]), format="json").json()["id"]
    att.post(f"/api/shifts/{sid}/end/", {**body(fc, [1100, 5040]), "cash": cash("200")}, format="json")
    end_id = stage(att)["end_id"]
    mgr.post(f"/api/shift-ends/{end_id}/send-back/", {"note": "Check pump 1"}, format="json")
    fix = att.post(f"/api/shift-ends/{end_id}/fix/", {"readings": [{"nozzle": str(fc.nozzles[0].pk), "reading": "990"}]}, format="json")
    assert fix.status_code == 400 and "below the number you started from" in str(fix.json())


# ---- what the manager sees on an end, and approving it --------------------------------------------------------------

def test_a_big_cash_difference_is_explained_on_the_end_and_needs_a_reason_to_approve():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    sid = att.post("/api/shifts/start/", body(fc, [1000, 5000]), format="json").json()["id"]
    att.post(f"/api/shifts/{sid}/end/", {**body(fc, [1100, 5040]), "cash": cash("637")}, format="json")  # 210 expected
    held = stage(att)
    assert held["cash_difference"] == "427.00"
    assert any("427.00 more than the meters say" in c for c in held["checks"])  # the attendant sees it too
    card = mgr.get("/api/shift-ends/").json()[0]
    assert card["checks"] == held["checks"]
    refused = mgr.post(f"/api/shift-ends/{card['id']}/approve/", {}, format="json")
    assert refused.status_code == 400 and "say why you approve it" in str(refused.json())
    ok = mgr.post(f"/api/shift-ends/{card['id']}/approve/", {"note": "Counted again with her: the cash is right, an end number was wrong"}, format="json")
    assert ok.status_code == 200 and ok.json()["status"] == "approved" and ok.json()["note"].startswith("Counted again")
    log = mgr.get("/api/approvals/log/").json()
    assert any(r["what"].endswith(": Counted again with her: the cash is right, an end number was wrong") for r in log)


def test_an_end_that_repeats_an_old_reading_is_pointed_out_when_the_cash_is_over():
    """Success Gapa's night: her end repeated the 22:59 day-close number, so her cash looked $427 over."""
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    now = timezone.now()
    with db_context(tenant_id=a["tenant"].id):
        shift = services.start_shift(user=a[Role.ATTENDANT], station=fc.station, started_at=now - timedelta(hours=9),
                                     readings=[{"nozzle": fc.nozzles[0], "reading": Decimal(0)}])
        services.add_checkpoint(user=a[Role.MANAGER], shift=shift, nozzle=fc.nozzles[0], reading=Decimal(100), at=now - timedelta(hours=8))
        services.end_shift(user=a[Role.ATTENDANT], shift=shift, ended_at=now - timedelta(minutes=5),
                           readings=[{"nozzle": fc.nozzles[0], "reading": Decimal(100)}], cash=[{"payment_type": "cash", "currency": "USD", "amount": Decimal(300)}])
    checks = stage(client_for(a[Role.ATTENDANT]))["checks"]
    assert any(f"the end number is the same as {a[Role.MANAGER].full_name}'s reading" in c and "for the last 7 hours" in c for c in checks), checks
    assert any("150.00 more than the meters say" in c for c in checks)


def test_the_manager_records_receiving_the_cash():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    sid = att.post("/api/shifts/start/", body(fc, [1000, 5000]), format="json").json()["id"]
    att.post(f"/api/shifts/{sid}/end/", {**body(fc, [1100, 5040]), "cash": cash("210")}, format="json")
    end_id = stage(att)["end_id"]
    assert att.post(f"/api/shift-ends/{end_id}/cash-received/").status_code == 403
    assert client_for(b[Role.MANAGER]).post(f"/api/shift-ends/{end_id}/cash-received/").status_code == 404
    card = mgr.post(f"/api/shift-ends/{end_id}/cash-received/").json()
    assert card["cash_received_by"] == a[Role.MANAGER].full_name and card["cash_received_at"]
    again = client_for(a[Role.OWNER]).post(f"/api/shift-ends/{end_id}/cash-received/").json()
    assert again["cash_received_by"] == a[Role.MANAGER].full_name  # received once
    assert any(r["what"] == f"Received {a[Role.ATTENDANT].full_name}'s cash (210.00)" for r in mgr.get("/api/approvals/log/").json())
    with db_context(tenant_id=a["tenant"].id):
        assert AuditLog.objects.filter(action="cash_received").count() == 1


# ---- handovers ------------------------------------------------------------------------------------------------------

def test_an_incoming_attendant_who_finds_another_number_on_the_pump_raises_an_alert_until_it_is_corrected():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    now = timezone.now()
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [500, 0], started_at=now - timedelta(hours=12), ended_at=now - timedelta(hours=2),
              cash=[("cash", "USD", 750)])
    att = client_for(a[Role.ATTENDANT])
    start = {"station": str(fc.station.pk), "readings": [{"nozzle": str(fc.nozzles[0].pk), "reading": "500", "seen": "100"},
                                                         {"nozzle": str(fc.nozzles[1].pk), "reading": "0"}]}
    assert att.post("/api/shifts/start/", start, format="json").status_code == 201
    found = open_alerts(a["tenant"], "meter")
    assert len(found) == 1 and "found 100.00 on the pump" in next(iter(found.values())) and "handover number is 500.00" in next(iter(found.values()))
    with db_context(tenant_id=a["tenant"].id):  # the handover was mistyped: corrected, with the start that followed it
        PumpMeterReading.objects.filter(nozzle=fc.nozzles[0], reading=Decimal(500)).update(reading=Decimal(100))
    assert open_alerts(a["tenant"], "meter") == {}


def test_meter_numbers_out_of_order_are_alerted_with_who_entered_them():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    now = timezone.now()
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [400, 0], started_at=now - timedelta(hours=20), ended_at=now - timedelta(hours=12),
              cash=[("cash", "USD", 600)])
    with db_context(tenant_id=a["tenant"].id):  # the next shift is entered from a lower number (the 400 was a typo for 300)
        shift = services.start_shift(user=a[Role.MANAGER], station=fc.station, started_at=now - timedelta(hours=10), attendants=[a[Role.ATTENDANT]],
                                     readings=[{"nozzle": fc.nozzles[0], "reading": Decimal(300)}, {"nozzle": fc.nozzles[1], "reading": Decimal(0)}])
    found = open_alerts(a["tenant"], "meter")
    assert len(found) == 1
    message = next(iter(found.values()))
    assert "start 300.00" in message and f"{a[Role.ATTENDANT].full_name}'s end 400.00" in message and "one of them is wrong" in message
    with db_context(tenant_id=a["tenant"].id):
        PumpMeterReading.objects.filter(shift=shift, nozzle=fc.nozzles[0]).update(reading=Decimal(400))
    assert open_alerts(a["tenant"], "meter") == {}


# ---- assigned pumps: a part left open -------------------------------------------------------------------------------

def test_no_pump_is_handed_at_once_to_someone_whose_earlier_part_is_still_open():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    now = timezone.now()
    ca, cb = client_for(att_a), client_for(att_b)
    ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": (now - timedelta(hours=20)).isoformat(), "readings": readings(fc.nozzles, [0, 0])}, format="json")
    cb.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": (now - timedelta(hours=2)).isoformat(), "readings": readings(p2, [0, 0])}, format="json")
    refused = mgr.post(f"/api/pumps/{pump2.pk}/assign/", {"user": str(att_a.pk), "readings": readings(p2, [10, 0])}, format="json")
    assert refused.status_code == 400 and "is still open" in str(refused.json())


def test_the_open_part_alert_stays_until_the_cash_is_handed_in():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    now = timezone.now()
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": (now - timedelta(hours=20)).isoformat(), "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    assert "has not ended their part" in str(open_alerts(a["tenant"], "missed_reading"))
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": (now - timedelta(hours=4)).isoformat(), "readings": readings(fc.nozzles, [50, 0]), "cash_later": True}, format="json")
    assert "has not handed in their cash" in str(open_alerts(a["tenant"], "missed_reading"))  # meters closed is not the end of it
    ca.post(f"/api/shifts/{sid}/cash/", {"cash": cash("75")}, format="json")
    assert not any(k.startswith("openpart") for k in open_alerts(a["tenant"], "missed_reading"))


# ---- deliveries -------------------------------------------------------------------------------------------------------

def test_a_tank_that_rises_with_no_delivery_or_more_than_the_delivery_note_is_alerted():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    now = timezone.now()
    dip(a["tenant"], a[Role.OWNER], fc, 2000, now - timedelta(hours=10))
    dip(a["tenant"], a[Role.OWNER], fc, 8000, now - timedelta(hours=6))  # 6000 L came in, nothing recorded
    found = open_alerts(a["tenant"], "delivery")
    assert len(found) == 1 and "about 6,000 L came in" in next(iter(found.values())) and "no delivery is recorded" in next(iter(found.values()))
    mgr = client_for(a[Role.MANAGER])
    delivered = {"tank": str(fc.tank.pk), "litres": "5000", "delivered_at": (now - timedelta(hours=8)).isoformat(), "client_id": str(uuid.uuid4())}
    assert mgr.post("/api/deliveries/", delivered, format="json").status_code == 201
    found = open_alerts(a["tenant"], "delivery")  # recorded, but the note says 5000 and the dips say 6000
    assert len(found) == 1 and "5,000 L delivered" in next(iter(found.values())) and "about 6,000 L came in" in next(iter(found.values()))
    with db_context(tenant_id=a["tenant"].id):
        from apps.operations.models import Delivery

        Delivery.objects.update(litres_received=Decimal(6000))  # the note was misread
    assert open_alerts(a["tenant"], "delivery") == {}


# ---- a day close with nobody on the pump ----------------------------------------------------------------------------

def test_after_a_day_close_with_nobody_on_the_pump_the_next_start_comes_from_the_day_close():
    """Usheokunze, Tue 3 Nov: 4B sold about 99 L after Latifah ended at 18:46 and before the 01:13 day close, with nobody
    on shift. Abel's morning start came from Latifah's number, so those litres were counted as his."""
    from apps.operations import dayclose

    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    now = timezone.now()
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [400, 0], started_at=now - timedelta(hours=20), ended_at=now - timedelta(hours=14),
              cash=[("cash", "USD", 600)])
    with db_context(tenant_id=a["tenant"].id):
        dayclose.close_day(user=a[Role.MANAGER], station=fc.station, dips=[{"tank": fc.tank, "litres": Decimal(5000)}],
                           meters=[{"nozzle": fc.nozzles[0], "reading": Decimal(450)}, {"nozzle": fc.nozzles[1], "reading": Decimal(0)}], at=now - timedelta(hours=10))
    att = client_for(a[Role.ATTENDANT])
    nozzles = {n["id"]: n for p in att.get("/api/shifts/prefill/").json()["stations"][0]["pumps"] for n in p["nozzles"]}
    assert nozzles[str(fc.nozzles[0].pk)]["last_reading"] == "450.00"  # the phone prefills the day close
    sid = att.post("/api/shifts/start/", body(fc, [400, 0]), format="json").json()["id"]  # a phone still showing the old number
    with db_context(tenant_id=a["tenant"].id):
        assert PumpMeterReading.objects.get(shift_id=sid, nozzle=fc.nozzles[0], kind="start").reading == Decimal(450)
