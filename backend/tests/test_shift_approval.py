"""An attendant's end of shift waits for a manager; changing what was handed in needs the owner's permission."""
from datetime import date

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.insights.models import OutboundMessage
from apps.operations.models import PumpAssignment, ShiftAttendant, Transaction, UndoRequest
from apps.stations.models import Pump, StationAssignment

from .helpers import at, client_for, make_forecourt, make_user, platform_client, run_shift, two_tenants


def body(fc, readings):
    return {"station": str(fc.station.pk), "readings": [{"nozzle": str(n.pk), "reading": str(r)} for n, r in zip(fc.nozzles, readings, strict=False)]}


def start(client, fc, readings=(1000, 5000)):
    return client.post("/api/shifts/start/", body(fc, readings), format="json")


def end(client, fc, sid, readings=(1100, 5040), cash="210"):
    payload = {**body(fc, readings), "cash": [{"payment_type": "cash", "currency": "USD", "amount": cash}]}
    return client.post(f"/api/shifts/{sid}/end/", payload, format="json")


def stage(client):
    return client.get("/api/shifts/prefill/").json()["stations"][0]["my_stage"]


def ended(a, fc, cash="210"):
    """The attendant starts and ends a shift (100 + 40 litres at 1.50 = 210.00 expected). Returns (shift id, end id)."""
    att = client_for(a[Role.ATTENDANT])
    sid = start(att, fc).json()["id"]
    assert end(att, fc, sid, cash=cash).status_code == 200
    return sid, stage(att)["end_id"]


def test_the_end_waits_for_the_manager_and_no_new_shift_starts_until_it_is_approved():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    sid, end_id = ended(a, fc)
    held = stage(att)
    assert held["stage"] == "held" and held["status"] == "pending" and held["expected"] == "210.00" and held["cash_difference"] == "0.00"
    assert [m["litres"] for m in held["meters"]] == ["100.000", "40.000"]
    again = start(att, fc, (1100, 5040))
    assert again.status_code == 400 and "waiting for the manager" in str(again.json())

    assert att.get("/api/shift-ends/").status_code == 403  # attendants never approve
    assert att.post(f"/api/shift-ends/{end_id}/approve/").status_code == 403
    assert client_for(b[Role.MANAGER]).post(f"/api/shift-ends/{end_id}/approve/").status_code == 404  # another business
    assert mgr.get("/api/approvals/summary/").json() == {"ends": 1, "undos": 0}
    ends = mgr.get("/api/shift-ends/").json()
    assert [(e["id"], e["person"], e["status"]) for e in ends] == [(end_id, a[Role.ATTENDANT].full_name, "pending")]

    assert mgr.post(f"/api/shift-ends/{end_id}/approve/").json()["status"] == "approved"
    assert stage(att)["stage"] == "start"
    assert start(att, fc, (1100, 5040)).status_code == 201
    with db_context(tenant_id=a["tenant"].id):
        assert AuditLog.objects.filter(action="shift_end_approved", actor=a[Role.MANAGER]).exists()


def test_a_sent_back_end_is_fixed_by_the_attendant_without_asking_the_owner():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    sid, end_id = ended(a, fc, cash="150")
    assert mgr.post(f"/api/shift-ends/{end_id}/send-back/", {}, format="json").status_code == 400  # say what to fix
    assert mgr.post(f"/api/shift-ends/{end_id}/send-back/", {"note": "Recount the cash"}, format="json").json()["status"] == "sent_back"
    held = stage(att)
    assert held["status"] == "sent_back" and held["note"] == "Recount the cash" and held["can_fix"] == {"cash": True, "meters": True}
    assert "sent back" in str(start(att, fc, (1100, 5040)).json())

    fix = {"cash": [{"payment_type": "cash", "currency": "USD", "amount": "210"}], "readings": [{"nozzle": str(fc.nozzles[0].pk), "reading": "1090"}]}
    res = att.post(f"/api/shift-ends/{end_id}/fix/", fix, format="json")
    assert res.status_code == 200, res.content
    assert res.json()["status"] == "pending"  # back with the manager
    summary = mgr.get(f"/api/shifts/{sid}/").json()["summary"]
    assert summary["expected"] == "195.00" and summary["submitted_total"] == "210.00"  # (90 + 40) x 1.50, rebuilt
    more = att.post(f"/api/shift-ends/{end_id}/fix/", {"cash": [{"payment_type": "cash", "currency": "USD", "amount": "200"}]}, format="json")
    assert more.status_code == 400 and "Ask to undo" in str(more.json())  # the fix is used up


def test_a_sent_back_end_checked_and_sent_again_unchanged_goes_back_to_the_manager():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    sid, end_id = ended(a, fc)
    mgr.post(f"/api/shift-ends/{end_id}/send-back/", {"note": "Check your meter"}, format="json")
    same = {"cash": [{"payment_type": "cash", "currency": "USD", "amount": "210"}], "readings": [{"nozzle": str(fc.nozzles[0].pk), "reading": "1100"}]}
    res = att.post(f"/api/shift-ends/{end_id}/fix/", same, format="json")
    assert res.status_code == 200 and res.json()["status"] == "pending", res.content  # "it was right": the manager sees it again
    meters_only = att.post(f"/api/shift-ends/{end_id}/fix/", {"readings": same["readings"]}, format="json")
    assert meters_only.status_code == 400 and "Nothing changed" in str(meters_only.json())  # not sent back any more


def test_changing_what_was_handed_in_needs_permission_once():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, mgr, owner = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER]), client_for(a[Role.OWNER])
    sid, end_id = ended(a, fc, cash="150")
    cash = lambda amount: att.post(f"/api/shifts/{sid}/cash/", {"cash": [{"payment_type": "cash", "currency": "USD", "amount": amount}]}, format="json")  # noqa: E731
    assert cash("210").status_code == 400
    assert cash("150").status_code == 200  # the same numbers again (a phone retrying) are fine

    assert att.post(f"/api/shift-ends/{end_id}/undo/", {"kind": "cash", "reason": ""}, format="json").status_code == 400
    asked = att.post(f"/api/shift-ends/{end_id}/undo/", {"kind": "cash", "reason": "Found 60 more in the drawer"}, format="json")
    assert asked.status_code == 201 and stage(att)["undo"]["status"] == "pending"
    assert len(mgr.get("/api/undo-requests/").json()) == 1  # managers answer requests at their stations too
    assert mgr.get("/api/approvals/summary/").json()["undos"] == 1
    listed = owner.get("/api/undo-requests/").json()
    assert [(u["person"], u["kind"], u["reason"]) for u in listed] == [(a[Role.ATTENDANT].full_name, "cash", "Found 60 more in the drawer")]
    assert owner.get("/api/approvals/summary/").json()["undos"] == 1

    assert owner.post(f"/api/undo-requests/{asked.json()['id']}/allow/", {"note": "ok"}, format="json").json()["status"] == "allowed"
    assert stage(att)["can_fix"] == {"cash": True, "meters": False}
    assert cash("210").status_code == 200
    assert cash("220").status_code == 400  # allowed once
    with db_context(tenant_id=a["tenant"].id):
        assert UndoRequest.objects.get().status == "used"
        assert ShiftAttendant.objects.get(shift_id=sid).approval == "pending"  # the change goes back to the manager
        assert OutboundMessage.objects.filter(kind="undo_request").exists()  # the owner was told (fails soft: no phone set)
        assert {e.action for e in AuditLog.objects.filter(action__in=["undo_requested", "undo_allowed", "cash_corrected"])} == {"undo_requested", "undo_allowed", "cash_corrected"}


def test_the_owner_can_refuse_and_the_attendant_sees_why():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, owner = client_for(a[Role.ATTENDANT]), client_for(a[Role.OWNER])
    _, end_id = ended(a, fc)
    req = att.post(f"/api/shift-ends/{end_id}/undo/", {"kind": "meters", "reason": "Typed 1100, it was 1010"}, format="json").json()
    assert owner.post(f"/api/undo-requests/{req['id']}/refuse/", {"note": "The photo shows 1100"}, format="json").json()["status"] == "refused"
    held = stage(att)
    assert held["undo"]["status"] == "refused" and held["undo"]["decision_note"] == "The photo shows 1100"
    assert held["can_fix"] == {"cash": False, "meters": False}


def test_ended_by_mistake_puts_them_back_on_the_shift_when_the_owner_allows_it():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, owner = client_for(a[Role.ATTENDANT]), client_for(a[Role.OWNER])
    sid, end_id = ended(a, fc)
    assert owner.get(f"/api/shifts/{sid}/").json()["status"] == "closed"
    req = att.post(f"/api/shift-ends/{end_id}/undo/", {"kind": "reopen", "reason": "I pressed End at lunch"}, format="json").json()
    assert owner.post(f"/api/undo-requests/{req['id']}/allow/").status_code == 200
    detail = owner.get(f"/api/shifts/{sid}/").json()
    assert detail["status"] == "open" and detail["transactions"] == [] and detail["cash"] == []
    on = stage(att)
    assert on["stage"] == "on_shift" and on["shift"]["id"] == sid
    assert end(att, fc, sid, readings=(1200, 5040), cash="360").status_code == 200  # the real end, later
    with db_context(tenant_id=a["tenant"].id):
        assert sum(t.amount for t in Transaction.objects.filter(shift_id=sid)) == 360  # (200 + 40) x 1.50
        assert UndoRequest.objects.get().status == "used"


def test_a_manager_ending_for_an_attendant_approves_it_at_once():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    sid = start(att, fc).json()["id"]
    payload = {**body(fc, (1100, 5040)), "cash": [{"payment_type": "cash", "currency": "USD", "amount": "210"}], "user": str(a[Role.ATTENDANT].pk)}
    assert mgr.post(f"/api/shifts/{sid}/end/", payload, format="json").status_code == 200
    assert stage(att)["stage"] == "start"


def test_assigned_pumps_pass_on_at_once_while_the_end_waits():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    first, second = a[Role.ATTENDANT], make_user(a["tenant"], Role.ATTENDANT, full_name="Second")
    platform = platform_client()
    assert platform.patch(f"/api/platform/tenants/{a['tenant'].id}/features/", {"multi_attendant_enabled": True, "pump_assignment_enabled": True}, format="json").status_code == 200
    assert platform.patch(f"/api/platform/tenants/{a['tenant'].id}/stations/{fc.station.pk}/policy/", {"attendants_mode": "multiple", "assignment_mode": "assigned"}, format="json").status_code == 200
    with db_context(tenant_id=a["tenant"].id):
        Pump.objects.filter(pk=fc.pump.pk).update(assigned_to=first)
    sid, _ = ended(a, fc)
    with db_context(tenant_id=a["tenant"].id):
        assert not PumpAssignment.objects.filter(pump=fc.pump, ended_at__isnull=True).exists()  # released
        Pump.objects.filter(pk=fc.pump.pk).update(assigned_to=second)
    assert stage(client_for(first))["status"] == "pending"
    assert start(client_for(second), fc, (1100, 5040)).status_code == 201  # the next attendant takes the pump
    assert start(client_for(first), fc, (1100, 5040)).status_code == 400


def test_the_owner_filters_sales_by_station():
    a, _ = two_tenants()
    main, other = make_forecourt(a["tenant"], price="1.00"), make_forecourt(a["tenant"], price="1.00", name="Avondale", code="DIESEL")
    run_shift(a["tenant"], a[Role.ATTENDANT], main, [0, 0], [100, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14))
    run_shift(a["tenant"], a[Role.ATTENDANT], other, [0, 0], [30, 0], started_at=at(2026, 10, 1, 15), ended_at=at(2026, 10, 1, 18))  # 20:00 Harare: before the 21:00 close
    owner = client_for(a[Role.OWNER])
    day = date(2026, 10, 1).isoformat()
    url = f"/api/reports/daily-sales/?date_from={day}&date_to={day}"
    assert owner.get(url).json()["totals"]["litres"] == "130.000"
    assert owner.get(f"{url}&station={other.station.pk}").json()["totals"]["litres"] == "30.000"
    assert owner.get(f"{url}&station={main.station.pk}").json()["totals"]["litres"] == "100.000"


def test_a_manager_answers_undo_requests_at_their_station_and_sees_what_was_done():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    elsewhere = make_forecourt(a["tenant"], name="Other", code="DIESEL")
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    other_mgr = make_user(a["tenant"], Role.MANAGER, full_name="Other Manager")
    with db_context(tenant_id=a["tenant"].id):
        StationAssignment.objects.create(tenant_id=a["tenant"].id, user=other_mgr, station=elsewhere.station)
    sid, end_id = ended(a, fc, cash="150")
    asked = att.post(f"/api/shift-ends/{end_id}/undo/", {"kind": "cash", "reason": "Cash at hand"}, format="json").json()

    om = client_for(other_mgr)  # runs another station: sees and decides none of it
    assert om.get("/api/undo-requests/").json() == [] and om.get("/api/approvals/summary/").json()["undos"] == 0
    assert om.post(f"/api/undo-requests/{asked['id']}/allow/").status_code == 404
    assert om.get("/api/approvals/log/").json() == []

    assert mgr.post(f"/api/undo-requests/{asked['id']}/allow/", {"note": "Counted it with him"}, format="json").json()["status"] == "allowed"
    log = mgr.get("/api/approvals/log/").json()
    assert [r["action"] for r in log] == ["undo_allowed", "undo_requested"]
    assert log[0]["by"] == a[Role.MANAGER].full_name and "Counted it with him" in log[0]["what"] and log[0]["station"] == fc.station.name
    assert att.get("/api/approvals/log/").status_code == 403  # attendants never see it
