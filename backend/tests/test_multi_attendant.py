"""Several attendants (PLAN.md section 4): joining, leaving, per-person cash, closing, readings that differ,
assigned pumps (manager-only assignment, each attendant ending their part, handovers, a swap), and two phones offline."""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.operations.models import CashSubmission, PumpMeterReading, Shift, ShiftAttendant, Transaction
from apps.stations.models import Nozzle, Pump

from .helpers import client_for, make_forecourt, make_user, platform_client, two_tenants

T0, T_SWAP, T_END = "2026-10-01T06:00:00Z", "2026-10-01T10:00:00Z", "2026-10-01T14:00:00Z"


def setup(policy="single", price="1.50"):
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price=price)
    if policy != "single":
        c = platform_client()
        assigned = policy == "assigned"
        assert c.patch(f"/api/platform/tenants/{a['tenant'].id}/features/", {"multi_attendant_enabled": True, "pump_assignment_enabled": assigned}, format="json").status_code == 200
        body = {"attendants_mode": "multiple", **({"assignment_mode": "assigned"} if assigned else {})}
        assert c.patch(f"/api/platform/tenants/{a['tenant'].id}/stations/{fc.station.pk}/policy/", body, format="json").status_code == 200
    b_user = make_user(a["tenant"], Role.ATTENDANT, full_name="Second Attendant")
    return a, fc, a[Role.ATTENDANT], b_user


def readings(nozzles, values):
    return [{"nozzle": str(n.pk), "reading": str(v)} for n, v in zip(nozzles, values, strict=True)]


def cash(amount, currency="USD", kind="cash"):
    return [{"payment_type": kind, "currency": currency, "amount": str(amount)}]


def start(client, fc, values, **extra):
    return client.post("/api/shifts/start/", {"station": str(fc.station.pk), "readings": readings(fc.nozzles, values), **extra}, format="json")


def op(kind, payload):
    return {"op_id": str(uuid.uuid4()), "kind": kind, "payload": payload}


# ---- one attendant per shift (the default): handover only -------------------------------------------------------

def test_single_attendant_station_allows_a_handover_but_not_two_people_at_once():
    a, fc, att_a, att_b = setup("single")
    ca, cb = client_for(att_a), client_for(att_b)
    sid = start(ca, fc, [0, 0]).json()["id"]
    refused = cb.post(f"/api/shifts/{sid}/join/", {}, format="json")
    assert refused.status_code == 400 and "one attendant" in str(refused.json())
    assert start(ca, fc, [0, 0], attendants=[str(att_b.pk)]).status_code == 400  # cannot name a second person either
    assert ca.post(f"/api/shifts/{sid}/leave/", {}, format="json").status_code == 200
    joined = cb.post(f"/api/shifts/{sid}/join/", {}, format="json").json()
    assert {p["name"]: p["is_lead"] for p in joined["attendants"] if p["left_at"] is None} == {"Second Attendant": True}
    done = cb.post(f"/api/shifts/{sid}/end/", {"readings": readings(fc.nozzles, [100, 0]), "cash": cash("148")}, format="json").json()
    assert done["status"] == "closed" and done["summary"]["status"] == "ok"  # the person who left does not block closing
    with db_context(tenant_id=a["tenant"].id):
        assert set(AuditLog.objects.values_list("action", flat=True)) >= {"shift_left", "shift_joined", "shift_closed"}


# ---- several attendants, shared pumps ---------------------------------------------------------------------------

def test_shift_closes_only_when_everyone_has_handed_in_and_shows_each_persons_cash():
    a, fc, att_a, att_b = setup("multiple")
    ca, cb = client_for(att_a), client_for(att_b)
    sid = start(ca, fc, [0, 0]).json()["id"]
    assert cb.post(f"/api/shifts/{sid}/join/", {}, format="json").status_code == 200
    first = ca.post(f"/api/shifts/{sid}/end/", {"readings": readings(fc.nozzles, [100, 40]), "cash": cash("100")}, format="json").json()
    assert first["status"] == "open" and first["pending"] == ["Second Attendant"] and first["close_requested_at"]
    assert cb.post(f"/api/shifts/{sid}/close/", {}, format="json").status_code == 403  # only the lead or a manager closes
    d = cb.post(f"/api/shifts/{sid}/cash/", {"cash": cash("110", kind="mobile_money")}, format="json").json()
    assert d["status"] == "closed" and d["pending"] == []
    s = d["summary"]
    assert (s["expected"], s["submitted_total"], s["status"]) == ("210.00", "210.00", "ok")  # (100 + 40) x 1.50
    assert {p["name"]: p["submitted"] for p in s["by_attendant"]} == {att_a.full_name: "100.00", "Second Attendant": "110.00"}
    assert s["per_attendant_expected"] is False and "expected" not in s["by_attendant"][0]
    assert {(c["attendant_name"], c["payment_type"]) for c in d["cash"]} == {(att_a.full_name, "cash"), ("Second Attendant", "mobile_money")}


def test_a_manager_can_mark_someone_as_left_so_the_shift_can_close():
    a, fc, att_a, att_b = setup("multiple")
    ca = client_for(att_a)
    sid = start(ca, fc, [0, 0], attendants=[str(att_b.pk)]).json()["id"]
    assert ca.post(f"/api/shifts/{sid}/end/", {"readings": readings(fc.nozzles, [10, 0]), "cash": cash("15")}, format="json").json()["status"] == "open"
    d = client_for(a[Role.MANAGER]).post(f"/api/shifts/{sid}/leave/", {"user": str(att_b.pk)}, format="json").json()
    assert d["status"] == "closed"
    assert client_for(att_b).post(f"/api/shifts/{sid}/leave/", {"user": str(att_a.pk)}, format="json").status_code in (403, 404)


def test_meter_numbers_are_entered_once_and_a_different_second_number_is_kept_and_flagged():
    a, fc, att_a, att_b = setup("multiple")
    ca, cb, mgr = client_for(att_a), client_for(att_b), client_for(a[Role.MANAGER])
    sid = start(ca, fc, [0, 0]).json()["id"]
    cb.post(f"/api/shifts/{sid}/join/", {}, format="json")
    ca.post(f"/api/shifts/{sid}/end/", {"readings": readings(fc.nozzles, [100, 40]), "cash": cash("150"), "close": False}, format="json")
    cb.post(f"/api/shifts/{sid}/end/", {"readings": readings(fc.nozzles, [120, 40]), "cash": cash("60")}, format="json")  # nozzle 2 agrees
    d = ca.post(f"/api/shifts/{sid}/close/", {}, format="json").json()
    assert d["status"] == "closed"
    ends = [r for r in d["readings"] if r["kind"] == "end" and r["nozzle"] == str(fc.nozzles[0].pk)]
    assert sorted(r["reading"] for r in ends) == ["100.00", "120.00"] and all("readings_differ" in r["flags"] for r in ends)
    assert len([r for r in d["readings"] if r["kind"] == "end" and r["nozzle"] == str(fc.nozzles[1].pk)]) == 1  # same number: one row
    assert f"readings_differ:{fc.nozzles[0].pk}:end" in d["flags"]
    by_nozzle = {t["nozzle"]: t["litres"] for t in d["transactions"]}
    assert by_nozzle[str(fc.nozzles[0].pk)] == "100.000"  # the first number counts until someone resolves it
    assert cb.post(f"/api/shifts/{sid}/readings/resolve/", {"reading": ends[0]["id"]}, format="json").status_code == 403
    keep = next(r for r in ends if r["reading"] == "120.00")
    d = mgr.post(f"/api/shifts/{sid}/readings/resolve/", {"reading": keep["id"]}, format="json").json()
    assert {t["nozzle"]: t["litres"] for t in d["transactions"]}[str(fc.nozzles[0].pk)] == "120.000"
    assert not any(f.startswith("readings_differ") for f in d["flags"])
    with db_context(tenant_id=a["tenant"].id):
        assert PumpMeterReading.all_objects.filter(shift_id=sid, nozzle=fc.nozzles[0], kind="end").count() == 2  # nothing destroyed
        assert AuditLog.objects.filter(action="readings_resolved").exists()


def test_a_persons_own_correction_keeps_the_old_number():
    a, fc, att_a, _ = setup("single", price="1.00")
    ca = client_for(att_a)
    sid = start(ca, fc, [0, 0]).json()["id"]
    ca.post(f"/api/shifts/{sid}/end/", {"readings": readings(fc.nozzles, [100, 0]), "cash": cash("100")}, format="json")
    fix = {"readings": readings(fc.nozzles, [90, 0]), "cash": cash("90")}
    assert ca.post(f"/api/shifts/{sid}/end/", fix, format="json").status_code == 400  # handed in: not without permission
    mgr = client_for(a[Role.MANAGER])
    end_id = mgr.get(f"/api/shifts/{sid}/").json()["attendants"][0]["id"]
    assert mgr.post(f"/api/shift-ends/{end_id}/send-back/", {"note": "Check pump 1"}, format="json").status_code == 200
    d = ca.post(f"/api/shifts/{sid}/end/", fix, format="json").json()  # sent back: the person corrects their own numbers
    assert d["summary"]["expected"] == "90.00" and d["summary"]["submitted_total"] == "90.00"
    with db_context(tenant_id=a["tenant"].id):
        old = PumpMeterReading.all_objects.get(shift_id=sid, nozzle=fc.nozzles[0], kind="end", deleted_at__isnull=False)
        assert old.reading == Decimal("100.00")
        assert AuditLog.objects.filter(action="cash_corrected").exists()


# ---- assigned pumps ----------------------------------------------------------------------------------------------

def assigned_site():
    """Assigned pumps, two pumps: the manager gives Pump 1 to A and Pump 2 to B."""
    a, fc, att_a, att_b = setup("assigned")
    with db_context(tenant_id=a["tenant"].id):
        pump2 = Pump.objects.create(tenant_id=a["tenant"].id, station=fc.station, name="Pump 2")
        p2 = [Nozzle.objects.create(tenant_id=a["tenant"].id, pump=pump2, tank=fc.tank, number=n) for n in (1, 2)]
    mgr = client_for(a[Role.MANAGER])
    assert mgr.post(f"/api/pumps/{fc.pump.pk}/assign/", {"user": str(att_a.pk)}, format="json").status_code == 200
    assert mgr.post(f"/api/pumps/{pump2.pk}/assign/", {"user": str(att_b.pk)}, format="json").status_code == 200
    return a, fc, att_a, att_b, pump2, p2, mgr


def test_only_managers_and_owners_assign_pumps():
    a, fc, att_a, att_b = setup("assigned")
    ca = client_for(att_a)
    assert ca.post(f"/api/pumps/{fc.pump.pk}/assign/", {"user": str(att_a.pk)}, format="json").status_code == 403
    no_pump = start(ca, fc, [0, 0], started_at=T0)
    assert no_pump.status_code == 400 and "No pump is assigned" in str(no_pump.json())
    owner = client_for(a[Role.OWNER])
    res = owner.post(f"/api/pumps/{fc.pump.pk}/assign/", {"user": str(att_a.pk)}, format="json")
    assert res.status_code == 200 and res.json()["assigned_to_name"] == att_a.full_name
    assert owner.post(f"/api/pumps/{fc.pump.pk}/assign/", {"user": str(a[Role.MANAGER].pk)}, format="json").status_code == 400  # attendants only
    with db_context(tenant_id=a["tenant"].id):
        assert AuditLog.objects.filter(action="pump_assigned").count() == 1
    # starting no longer lets anyone choose pumps: an "assignments" map from an old phone is ignored
    started = start(ca, fc, [0, 0], started_at=T0, assignments={str(fc.pump.pk): str(att_b.pk)})
    assert started.status_code == 201 and [p["name"] for p in started.json()["attendants"]] == [att_a.full_name]


def test_attendants_see_only_their_own_pumps_and_share_one_shift():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    mine = ca.get("/api/shifts/prefill/").json()["stations"][0]
    assert [p["name"] for p in mine["pumps"]] == ["Pump 1"] and mine["joinable_shift"] is None
    assert [p["name"] for p in cb.get("/api/shifts/prefill/").json()["stations"][0]["pumps"]] == ["Pump 2"]
    every = mgr.get("/api/shifts/prefill/").json()["stations"][0]["pumps"]
    assert {(p["name"], p["assigned_to_name"]) for p in every} == {("Pump 1", att_a.full_name), ("Pump 2", "Second Attendant")}
    body = {"station": str(fc.station.pk), "started_at": T0}
    assert ca.post("/api/shifts/start/", {**body, "readings": readings([*fc.nozzles, p2[0]], [0, 0, 0])}, format="json").status_code == 400  # not A's pump
    assert ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles[:1], [0])}, format="json").status_code == 400  # every nozzle
    sa = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json")
    sb = cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    assert sa.status_code == 201 and sb.status_code == 201
    assert sb.json()["id"] == sa.json()["id"] and sb.json()["merged"] is True  # many attendants, one shift
    assert {p["name"] for p in sb.json()["attendants"]} == {att_a.full_name, "Second Attendant"}
    assert cb.get("/api/shifts/prefill/").json()["stations"][0]["open_shift_id"] == sa.json()["id"]


def test_each_attendant_ends_their_part_and_their_pumps_pass_to_the_incoming_attendant():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    att_c = make_user(a["tenant"], Role.ATTENDANT, full_name="Third Attendant")
    ca, cb, cc = client_for(att_a), client_for(att_b), client_for(att_c)
    body = {"station": str(fc.station.pk), "started_at": T0}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    # the manager plans the handover: Pump 1 goes to C next. A keeps it until A ends their part.
    planned = mgr.post(f"/api/pumps/{fc.pump.pk}/assign/", {"user": str(att_c.pk)}, format="json")
    assert planned.status_code == 200 and planned.json()["assigned_to_name"] == "Third Attendant"
    assert [p["name"] for p in ca.get("/api/shifts/prefill/").json()["stations"][0]["pumps"]] == ["Pump 1"]
    waiting = cc.get("/api/shifts/prefill/").json()["stations"][0]["pumps"]
    assert [(p["name"], p["in_use_by"]) for p in waiting] == [("Pump 1", att_a.full_name)]
    early = cc.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": T_SWAP, "readings": readings(fc.nozzles, [50, 0])}, format="json")
    assert early.status_code == 400 and "still with" in str(early.json())
    # A ends their part at 10:00: their numbers, their cash. The shift goes on (B is still on it).
    da = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_SWAP, "readings": readings(fc.nozzles, [50, 0]), "cash": cash("75")}, format="json")
    assert da.status_code == 200 and da.json()["status"] == "open", da.content
    # C, the incoming attendant, starts on Pump 1 at those numbers and joins the same shift
    sc = cc.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": "2026-10-01T10:05:00Z", "readings": readings(fc.nozzles, [50, 0])}, format="json")
    assert sc.status_code == 201 and sc.json()["id"] == sid
    assert cb.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(p2, [20, 0]), "cash": cash("30")}, format="json").json()["status"] == "open"
    d = cc.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("70")}, format="json").json()
    assert d["status"] == "closed", d  # the last person out closes the shift
    people = {p["name"]: p for p in d["summary"]["by_attendant"]}
    assert (people[att_a.full_name]["expected"], people[att_a.full_name]["cash_difference"]) == ("75.00", "0.00")
    assert (people["Second Attendant"]["expected"], people["Second Attendant"]["cash_difference"]) == ("30.00", "0.00")
    assert (people["Third Attendant"]["expected"], people["Third Attendant"]["cash_difference"]) == ("75.00", "-5.00")
    sold = {(r["pump"], r["attendant_name"]): r["litres"] for r in d["summary"]["by_pump"] if Decimal(r["litres"])}
    assert sold == {("Pump 1", att_a.full_name): "50.000", ("Pump 1", "Third Attendant"): "50.000", ("Pump 2", "Second Attendant"): "20.000"}
    report = {r["name"]: r for r in mgr.get("/api/reports/attendants/", {"date_from": "2026-10-01", "date_to": "2026-10-01"}).json()["rows"]}
    assert (report["Third Attendant"]["litres"], report["Third Attendant"]["handed_in"], report["Third Attendant"]["cash_difference"]) == ("50.000", "70.00", "-5.00")
    assert {(p["pump"], p["litres"]) for p in report[att_a.full_name]["pumps"]} == {("Pump 1", "50.000")}
    assert ca.get("/api/reports/attendants/").status_code == 403
    with db_context(tenant_id=a["tenant"].id):
        assert Shift.objects.filter(station=fc.station).count() == 1


def test_the_dashboard_shows_a_part_that_ended_while_the_shift_goes_on():
    """An assigned-pump shift stays open all day; each part's sales show as soon as that part ends."""
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    now = timezone.now()
    body = {"station": str(fc.station.pk), "started_at": (now - timedelta(minutes=10)).isoformat()}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    assert mgr.get("/api/dashboard/").json()["totals"]["today_litres"] == "0"
    ended = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": (now - timedelta(minutes=1)).isoformat(), "readings": readings(fc.nozzles, [50, 0]), "cash": cash("75")}, format="json")
    assert ended.status_code == 200 and ended.json()["status"] == "open", ended.content
    t = mgr.get("/api/dashboard/").json()["totals"]
    assert (t["today_litres"], t["today_amount_by_currency"], t["open_shifts"]) == ("50.000", {"USD": "75.00"}, 1)
    with db_context(tenant_id=a["tenant"].id):
        assert not [f for f in Shift.objects.get(pk=sid).flags if f.startswith("missing_end")]  # Pump 2 is simply still in use
    # the report by attendant shows the finished part now; B, still on, shows once their part ends
    rows = {r["name"]: r for r in mgr.get("/api/reports/attendants/").json()["rows"]}
    assert set(rows) == {att_a.full_name}
    mine = rows[att_a.full_name]
    assert (mine["litres"], mine["sales"], mine["handed_in"], mine["expected"], mine["cash_difference"]) == ("50.000", "75.00", "75.00", "75.00", "0.00")
    assert [(p["pump"], p["litres"]) for p in mine["pumps"]] == [("Pump 1", "50.000")]
    # the last person out closes the shift; the day then counts both parts once
    cb.post(f"/api/shifts/{sid}/end/", {"ended_at": now.isoformat(), "readings": readings(p2, [20, 0]), "cash": cash("30")}, format="json")
    assert mgr.get("/api/dashboard/").json()["totals"]["today_litres"] == "70.000"


def test_a_manager_can_swap_a_pump_at_once_with_the_meter_numbers():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": T0}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    swap = {"user": str(att_b.pk), "at": T_SWAP, "readings": readings(fc.nozzles, [50, 0])}
    assert ca.post(f"/api/pumps/{fc.pump.pk}/assign/", swap, format="json").status_code == 403  # attendants never swap
    assert cb.post(f"/api/shifts/{sid}/reassign/", {**swap, "pump": str(fc.pump.pk)}, format="json").status_code == 403
    short = mgr.post(f"/api/pumps/{fc.pump.pk}/assign/", {**swap, "readings": readings(fc.nozzles[:1], [50])}, format="json")
    assert short.status_code == 400 and "every nozzle" in str(short.json())
    assert mgr.post(f"/api/pumps/{fc.pump.pk}/assign/", swap, format="json").status_code == 200
    assert {p["name"] for p in cb.get("/api/shifts/prefill/").json()["stations"][0]["pumps"]} == {"Pump 1", "Pump 2"}
    assert ca.get("/api/shifts/prefill/").json()["stations"][0]["pumps"] == []
    # A: 50 L before the swap x 1.50 = 75. B: 50 L on Pump 1 after it + 20 L on Pump 2 = 105, hands in 100.
    assert ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": [], "cash": cash("75")}, format="json").json()["status"] == "open"
    d = cb.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings([*fc.nozzles, *p2], [100, 0, 20, 0]), "cash": cash("100")}, format="json").json()
    assert d["status"] == "closed", d
    people = {p["name"]: p for p in d["summary"]["by_attendant"]}
    assert (people[att_a.full_name]["expected"], people[att_a.full_name]["cash_difference"]) == ("75.00", "0.00")
    assert (people["Second Attendant"]["expected"], people["Second Attendant"]["cash_difference"]) == ("105.00", "-5.00")
    with db_context(tenant_id=a["tenant"].id):
        assert AuditLog.objects.filter(action="pump_reassigned").count() == 1
        holders = {(t.nozzle_id, t.started_at.hour): t.attendant_id for t in Transaction.objects.filter(shift_id=sid) if t.litres > 0}
        assert holders == {(fc.nozzles[0].pk, 6): att_a.pk, (fc.nozzles[0].pk, 10): att_b.pk, (p2[0].pk, 6): att_b.pk}


def test_a_phone_that_sends_end_numbers_for_a_pump_swapped_away_is_ignored_and_flagged():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": T0}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    mgr.post(f"/api/pumps/{fc.pump.pk}/assign/", {"user": str(att_b.pk), "at": T_SWAP, "readings": readings(fc.nozzles, [50, 0])}, format="json")
    res = ca.post("/api/sync/", {"ops": [op("shift_end", {"shift_id": sid, "ended_at": T_END, "readings": readings(fc.nozzles, [90, 0]), "cash": cash("75")})]}, format="json")
    result = res.json()["results"][0]
    assert result["status"] == "applied" and result["result"]["status"] == "open"
    assert any(f.startswith("reading_after_swap") for f in result["result"]["flags"])
    cb.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings([*fc.nozzles, *p2], [100, 0, 20, 0]), "cash": cash("105")}, format="json")
    with db_context(tenant_id=a["tenant"].id):
        assert Shift.objects.get(pk=sid).status == "closed"
        assert sum(t.litres for t in Transaction.objects.filter(shift_id=sid, attendant=att_a)) == Decimal("50")


# ---- two phones, offline -----------------------------------------------------------------------------------------

def _two_phone_shift(order):
    a, fc, att_a, att_b = setup("multiple")
    ca, cb = client_for(att_a), client_for(att_b)
    sid = start(ca, fc, [0, 0]).json()["id"]
    cb.post(f"/api/shifts/{sid}/join/", {}, format="json")
    # both phones go offline; each queues its own end-of-shift
    queue_a = [op("shift_end", {"shift_id": sid, "readings": readings(fc.nozzles, [100, 40]), "cash": cash("120")})]
    queue_b = [op("shift_cash", {"shift_id": sid, "cash": cash("90")})]
    results = {}
    for who in order:
        client, queue = (ca, queue_a) if who == "a" else (cb, queue_b)
        results[who] = client.post("/api/sync/", {"ops": queue}, format="json").json()["results"][0]
    return a, sid, results


def test_two_phones_sync_one_shift_lead_first():
    a, sid, r = _two_phone_shift(["a", "b"])
    assert r["a"]["status"] == "applied" and r["a"]["result"]["status"] == "open"  # waits for the second person
    assert r["b"]["result"]["status"] == "closed"
    with db_context(tenant_id=a["tenant"].id):
        shift = Shift.objects.get(pk=sid)
        assert sum(t.amount for t in Transaction.objects.filter(shift=shift)) == Decimal("210.00")
        assert sum(c.amount for c in CashSubmission.objects.filter(shift=shift)) == Decimal("210.00")


def test_two_phones_sync_one_shift_second_person_first():
    a, sid, r = _two_phone_shift(["b", "a"])
    assert r["b"]["result"]["status"] == "open" and r["a"]["result"]["status"] == "closed"
    with db_context(tenant_id=a["tenant"].id):
        assert Shift.objects.get(pk=sid).status == "closed"


def test_two_phones_that_both_started_offline_end_up_on_one_shift():
    a, fc, att_a, att_b = setup("multiple")
    sid_a, sid_b = str(uuid.uuid4()), str(uuid.uuid4())
    ca, cb = client_for(att_a), client_for(att_b)
    ra = ca.post("/api/sync/", {"ops": [op("shift_start", {"shift_id": sid_a, "station": str(fc.station.pk), "started_at": T0, "readings": readings(fc.nozzles, [0, 0])})]}, format="json").json()
    rb = cb.post("/api/sync/", {"ops": [
        op("shift_start", {"shift_id": sid_b, "station": str(fc.station.pk), "started_at": "2026-10-01T06:05:00Z", "readings": readings(fc.nozzles, [0, 0])}),
        op("shift_cash", {"shift_id": sid_b, "cash": cash("50")}),  # this phone still uses the id it made up
    ]}, format="json").json()
    assert ra["results"][0]["result"]["shift_id"] == sid_a
    assert rb["results"][0]["result"] == {"shift_id": sid_a, "merged": True, "client_shift_id": sid_b}
    assert rb["results"][1]["status"] == "applied" and rb["results"][1]["result"]["shift_id"] == sid_a
    done = ca.post("/api/sync/", {"ops": [op("shift_end", {"shift_id": sid_a, "ended_at": T_END, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("100")})]}, format="json").json()
    assert done["results"][0]["result"]["status"] == "closed"
    with db_context(tenant_id=a["tenant"].id):
        assert Shift.objects.filter(station=fc.station).count() == 1
        assert ShiftAttendant.objects.filter(shift_id=sid_a).count() == 2
        assert PumpMeterReading.objects.filter(shift_id=sid_a, kind="start").count() == 2  # same numbers: no duplicates


def test_an_attendant_cannot_join_a_shift_at_a_station_they_do_not_work_at():
    a, fc, att_a, att_b = setup("multiple")
    other = make_forecourt(a["tenant"], name="Elsewhere", code="DIESEL")
    client_for(a[Role.OWNER]).put(f"/api/users/{att_b.pk}/stations/", {"station_ids": [str(other.station.pk)]}, format="json")
    sid = start(client_for(att_a), fc, [0, 0]).json()["id"]
    assert client_for(att_b).post(f"/api/shifts/{sid}/join/", {}, format="json").status_code == 404


# ---- the data migration ------------------------------------------------------------------------------------------

def _migrate(target):
    executor = MigrationExecutor(connections["owner"])
    executor.loader.build_graph()
    executor.migrate([target])


def test_the_single_attendant_migration_moves_data_both_ways():
    a, fc, att_a, _ = setup("single")
    tid, shift_id = str(a["tenant"].id), str(uuid.uuid4())
    latest = MigrationExecutor(connections["owner"]).loader.graph.leaf_nodes("operations")[0]
    try:
        _migrate(("operations", "0004_tank_records_client_id_delivery_photo"))
        with transaction.atomic(using="owner"), connections["owner"].cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", [tid])
            cur.execute(
                "INSERT INTO operations_shift (id, created_at, updated_at, tenant_id, station_id, attendant_id, started_at, ended_at, status, flags, notes)"
                " VALUES (%s, now(), now(), %s, %s, %s, now() - interval '8 hours', now(), 'closed', '[]', '')",
                [shift_id, tid, str(fc.station.pk), str(att_a.pk)],
            )
            cur.execute(
                "INSERT INTO operations_cashsubmission (id, created_at, updated_at, tenant_id, shift_id, payment_type, currency, amount)"
                " VALUES (gen_random_uuid(), now(), now(), %s, %s, 'cash', 'USD', 10)", [tid, shift_id],
            )
        _migrate(latest)
        with db_context(tenant_id=a["tenant"].id):
            lead = ShiftAttendant.objects.get(shift_id=shift_id)
            assert lead.user_id == att_a.pk and lead.is_lead and lead.cash_submitted_at is not None
            assert CashSubmission.objects.get(shift_id=shift_id).attendant_id == att_a.pk
        _migrate(("operations", "0004_tank_records_client_id_delivery_photo"))
        with transaction.atomic(using="owner"), connections["owner"].cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", [tid])
            cur.execute("SELECT attendant_id FROM operations_shift WHERE id = %s", [shift_id])
            assert str(cur.fetchone()[0]) == str(att_a.pk)
    finally:
        _migrate(latest)


# ---- the stage a phone should show --------------------------------------------------------------------------------

def stage_of(client):
    return client.get("/api/shifts/prefill/").json()["stations"][0]["my_stage"]


def test_the_server_tells_any_phone_which_stage_an_attendant_is_at():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    assert stage_of(ca) == {"stage": "start"}
    body = {"station": str(fc.station.pk), "started_at": T0}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [120, 40])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    # signed out and in again, or on another phone: still on shift, with the numbers it started from
    on = stage_of(ca)
    assert on["stage"] == "on_shift" and on["shift"]["id"] == sid and on["shift"]["member"] is False
    assert on["shift"]["starts"] == {str(fc.nozzles[0].pk): "120.00", str(fc.nozzles[1].pk): "40.00"}
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_SWAP, "readings": readings(fc.nozzles, [150, 40]), "cash": cash("45")}, format="json")
    held = stage_of(ca)
    assert held["stage"] == "held" and held["status"] == "pending" and held["shift_id"] == sid  # waits for the manager
    assert mgr.post(f"/api/shift-ends/{held['end_id']}/approve/").status_code == 200
    assert stage_of(ca) == {"stage": "start"}  # their part is over while the shift goes on for B; they may start a new part
    assert stage_of(cb)["stage"] == "on_shift"


def test_after_an_approved_end_an_attendant_works_a_second_part_on_new_pumps():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    with db_context(tenant_id=a["tenant"].id):
        pump3 = Pump.objects.create(tenant_id=a["tenant"].id, station=fc.station, name="Pump 3")
        p3 = [Nozzle.objects.create(tenant_id=a["tenant"].id, pump=pump3, tank=fc.tank, number=n) for n in (1, 2)]
    ca, cb = client_for(att_a), client_for(att_b)
    body, later = {"station": str(fc.station.pk), "started_at": T0}, {"station": str(fc.station.pk), "started_at": "2026-10-01T11:00:00Z"}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [100, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_SWAP, "readings": readings(fc.nozzles, [150, 0]), "cash": cash("75")}, format="json")
    assert ca.post("/api/shifts/start/", {**later, "readings": readings(p3, [500, 0])}, format="json").status_code == 400  # not approved yet
    assert mgr.post(f"/api/shift-ends/{stage_of(ca)['end_id']}/approve/").status_code == 200
    assert mgr.post(f"/api/pumps/{pump3.pk}/assign/", {"user": str(att_a.pk)}, format="json").status_code == 200
    assert stage_of(ca) == {"stage": "start"}
    assert "Pump 3" in [p["name"] for p in ca.get("/api/shifts/prefill/").json()["stations"][0]["pumps"]]

    # Pump 1 is still theirs too: the phone asks for every pump they now have
    second = ca.post("/api/shifts/start/", {**later, "readings": readings([*fc.nozzles, *p3], [150, 0, 500, 0])}, format="json")
    assert second.status_code == 201, second.content
    assert second.json()["id"] == sid  # the same station shift, a second part
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": "2026-10-01T13:00:00Z", "readings": readings([*fc.nozzles, *p3], [150, 0, 520, 0]), "cash": cash("30")}, format="json")
    held = stage_of(ca)
    assert held["status"] == "pending" and held["cash"] == [{"payment_type": "cash", "currency": "USD", "amount": "30.00"}]  # this part only
    assert mgr.post(f"/api/shift-ends/{held['end_id']}/approve/").status_code == 200
    cb.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(p2, [10, 0]), "cash": cash("15")}, format="json")  # last: closes

    d = mgr.get(f"/api/shifts/{sid}/").json()
    assert d["status"] == "closed" and [p["name"] for p in d["attendants"]].count(att_a.full_name) == 2
    assert d["people"].count(att_a.full_name) == 1
    mine = [p for p in d["summary"]["by_attendant"] if p["user"] == str(att_a.pk)]
    assert len(mine) == 1 and mine[0]["submitted"] == "105.00" and mine[0]["expected"] == "105.00"  # (50 + 20) litres x 1.50
    with db_context(tenant_id=a["tenant"].id):
        assert CashSubmission.objects.filter(attendant=att_a).values("part").distinct().count() == 2  # both hand-ins kept


def test_on_a_one_attendant_station_the_stage_goes_back_to_start_after_the_shift():
    a, fc, att_a, _ = setup("single")
    ca = client_for(att_a)
    sid = start(ca, fc, [10, 20], started_at=T0).json()["id"]
    assert stage_of(ca)["shift"]["starts"] == {str(fc.nozzles[0].pk): "10.00", str(fc.nozzles[1].pk): "20.00"}
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(fc.nozzles, [30, 20]), "cash": cash("30")}, format="json")
    held = stage_of(ca)
    assert held["stage"] == "held" and held["status"] == "pending"
    assert client_for(a[Role.MANAGER]).post(f"/api/shift-ends/{held['end_id']}/approve/").status_code == 200
    assert stage_of(ca) == {"stage": "start"}



# ---- one attendant per shift: never two open shifts on the same pumps --------------------------------------------

def test_a_one_attendant_station_never_has_two_open_shifts():
    a, fc, att_a, att_b = setup("single")
    ca, cb = client_for(att_a), client_for(att_b)
    sid = start(ca, fc, [0, 0], started_at=T0).json()["id"]
    assert start(ca, fc, [0, 0], started_at=T0).json()["id"] == sid  # the same person on a second phone gets their shift back
    refused = start(cb, fc, [0, 0], started_at=T_SWAP)
    assert refused.status_code == 400 and att_a.full_name in str(refused.json())
    offline = cb.post("/api/sync/", {"ops": [op("shift_start", {"shift_id": str(uuid.uuid4()), "station": str(fc.station.pk), "started_at": T_SWAP, "readings": readings(fc.nozzles, [0, 0])})]}, format="json")
    assert offline.json()["results"][0]["status"] == "rejected"  # kept for a manager, never a second shift
    assert cb.get("/api/shifts/prefill/").json()["stations"][0]["busy_shift"] == {"people": [att_a.full_name]}
    # A leaves (handover): B starting now continues the same shift
    ca.post(f"/api/shifts/{sid}/leave/", {"at": T_SWAP}, format="json")
    handed = start(cb, fc, [40, 0], started_at=T_SWAP)
    assert handed.status_code == 201 and handed.json()["id"] == sid
    with db_context(tenant_id=a["tenant"].id):
        assert Shift.objects.filter(station=fc.station, status="open").count() == 1



def test_a_planned_pump_passes_straight_to_an_attendant_already_on_the_shift():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": T0}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    mgr.post(f"/api/pumps/{fc.pump.pk}/assign/", {"user": str(att_b.pk)}, format="json")  # planned: A keeps it until A ends
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_SWAP, "readings": readings(fc.nozzles, [50, 0]), "cash": cash("75")}, format="json")
    # B's phone now shows Pump 1, starting from A's end numbers
    starts = cb.get("/api/shifts/prefill/").json()["stations"][0]["my_stage"]["shift"]["starts"]
    assert starts[str(fc.nozzles[0].pk)] == "50.00" and str(p2[0].pk) in starts
    d = cb.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings([*fc.nozzles, *p2], [100, 0, 20, 0]), "cash": cash("105")}, format="json").json()
    assert d["status"] == "closed"
    people = {p["name"]: p for p in d["summary"]["by_attendant"]}
    assert people[att_a.full_name]["expected"] == "75.00" and people["Second Attendant"]["expected"] == "105.00"  # (50 + 20) x 1.50


def test_on_assigned_pumps_the_long_shift_alert_names_only_the_person_on_too_long():
    """The shift stays open all day on assigned pumps; only someone on their own part for too long is flagged."""
    from apps.insights.alerts import evaluate_tenant
    from apps.insights.models import Alert

    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    now = timezone.now()
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": (now - timedelta(hours=20)).isoformat(), "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": (now - timedelta(hours=2)).isoformat(), "readings": readings(p2, [0, 0])}, format="json")

    def missed():
        with db_context(tenant_id=a["tenant"].id):
            evaluate_tenant(a["tenant"].id)
            return {al.dedupe_key.split(":")[0]: al.message for al in Alert.objects.filter(kind="missed_reading", status="open") if al.dedupe_key.startswith(("openshift", "openpart"))}

    flagged = missed()
    assert list(flagged) == ["openpart"] and att_a.full_name in flagged["openpart"] and att_b.full_name not in flagged["openpart"]
    ca.post(f"/api/shifts/{sid}/end/", {"readings": readings(fc.nozzles, [50, 0]), "cash": cash("75")}, format="json")
    assert missed() == {}  # A ended their part: it clears by itself; B has been on 2 hours


def test_the_setup_checklist_counts_a_part_handed_in_while_the_shift_goes_on():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    owner, ca, cb = client_for(a[Role.OWNER]), client_for(att_a), client_for(att_b)

    def shift_item():
        return next(i for i in owner.get("/api/onboarding/checklist/").json()["items"] if i["key"] == "shift")["done"]

    body = {"station": str(fc.station.pk), "started_at": T0}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    assert not shift_item()
    ended = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_SWAP, "readings": readings(fc.nozzles, [50, 0]), "cash": cash("75")}, format="json")
    assert ended.json()["status"] == "open" and shift_item()  # B is still on, but A's part went end to end


def test_a_manager_starts_someone_already_on_shift_on_a_free_pump():
    """A free pump given to someone on shift starts now, at the numbers entered: it is on their phone and theirs to end."""
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca = client_for(att_a)
    att_c = make_user(a["tenant"], Role.ATTENDANT, full_name="Third Attendant")
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": T0, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    staff = {s["name"]: s["on_shift"] for s in mgr.get("/api/shifts/prefill/").json()["stations"][0]["staff"]}
    # Pump 2 is free (B never started). Someone not on shift cannot be started on it now: it is theirs from their next start.
    refused = mgr.post(f"/api/pumps/{pump2.pk}/assign/", {"user": str(att_c.pk), "readings": readings(p2, [10, 0])}, format="json")
    assert refused.status_code == 400 and "not on shift" in str(refused.json())
    # The manager gives it to A, who is on shift, with its meter numbers now.
    res = mgr.post(f"/api/pumps/{pump2.pk}/assign/", {"user": str(att_a.pk), "at": T_SWAP, "readings": readings(p2, [10, 0])}, format="json")
    assert res.status_code == 200, res.content
    assert sorted(p["name"] for p in ca.get("/api/shifts/prefill/").json()["stations"][0]["pumps"]) == ["Pump 1", "Pump 2"]
    assert staff[att_a.full_name] is True and staff["Third Attendant"] is False
    d = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(fc.nozzles, [50, 0]) + readings(p2, [30, 0]), "cash": cash("105")}, format="json").json()
    sold = {r["pump"]: r["litres"] for r in d["summary"]["by_pump"] if Decimal(r["litres"])}
    assert sold == {"Pump 1": "50.000", "Pump 2": "20.000"}  # Pump 2 counted from the 10 entered at the start


def test_a_start_that_reaches_the_server_late_keeps_the_persons_litres():
    """A phone queued a start while offline and the person started again later: when the old start arrives, their
    litres still count for them (it happened at Eastview on 21 Oct, where 384 L were credited to nobody)."""
    from django.utils.dateparse import parse_datetime

    from apps.operations import services

    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    cb.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": T0, "readings": readings(p2, [0, 0])}, format="json")  # B keeps the shift open
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": T_SWAP, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    with db_context(tenant_id=a["tenant"].id):  # A's start queued at 07:00 reaches the server now
        services.start_shift(user=att_a, station=fc.station, started_at=parse_datetime("2026-10-01T07:00:00Z"), shift_id=uuid.uuid4(),
                             readings=[{"nozzle": n, "reading": Decimal(0)} for n in fc.nozzles])
    d = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(fc.nozzles, [50, 0]), "cash": cash("75")}, format="json")
    assert d.status_code == 200, d.content
    with db_context(tenant_id=a["tenant"].id):
        mine = Transaction.objects.filter(shift_id=sid, attendant=att_a).aggregate(s=Sum("litres"))["s"]
        assert mine == Decimal("50.000")
        assert not Transaction.objects.filter(shift_id=sid, attendant__isnull=True, litres__gt=0).exists()


def test_close_my_meters_first_then_hand_in_the_cash_after_counting():
    """At the shift change the outgoing attendant closes their meters (their pumps pass on at once), then counts and
    hands in their cash against what the meters say. Until the cash is in they cannot start another shift."""
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": T0}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")

    closed = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_SWAP, "readings": readings(fc.nozzles, [50, 0]), "cash_later": True}, format="json")
    assert closed.status_code == 200 and closed.json()["status"] == "open", closed.content
    stage = ca.get("/api/shifts/prefill/").json()["stations"][0]["my_stage"]
    assert stage["stage"] == "cash_due" and stage["expected"] == "75.00" and [m["litres"] for m in stage["meters"]][0] == "50.000"
    refused = ca.post("/api/shifts/start/", {**body, "started_at": T_END, "readings": readings(fc.nozzles, [50, 0])}, format="json")
    assert refused.status_code == 400 and "not handed in their cash" in str(refused.json())
    waiting = {e["person"]: e for e in mgr.get("/api/shift-ends/").json()}
    assert waiting[att_a.full_name]["status"] == "cash_due"
    assert waiting[att_a.full_name]["ended_at"] is not None  # when they closed their meters (not a blank date)
    end_id = stage["end_id"]
    assert mgr.post(f"/api/shift-ends/{end_id}/approve/").status_code == 400  # nothing to approve until the cash is in

    handed = ca.post(f"/api/shifts/{sid}/cash/", {"cash": cash("75")}, format="json")
    assert handed.status_code == 200, handed.content
    assert ca.get("/api/shifts/prefill/").json()["stations"][0]["my_stage"]["stage"] == "held"  # now with the manager
    assert mgr.post(f"/api/shift-ends/{end_id}/approve/").status_code == 200

    # the last one out closes their meters: the shift closes, and their cash still lands on it afterwards
    last = cb.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(p2, [20, 0]), "cash_later": True}, format="json").json()
    assert last["status"] == "closed"
    assert cb.post(f"/api/shifts/{sid}/cash/", {"cash": cash("30")}, format="json").status_code == 200
    people = {p["name"]: p for p in mgr.get(f"/api/shifts/{sid}/").json()["summary"]["by_attendant"]}
    assert (people["Second Attendant"]["expected"], people["Second Attendant"]["submitted"]) == ("30.00", "30.00")


def test_the_day_close_ends_the_trading_day_at_about_21_00():
    """The manager dips every tank and reads every pump at about 21:00: that moment ends the trading day (named by the
    day it closes). Someone working through it keeps their part and their cash; their litres split between the days."""
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": "2026-10-01T15:00:00Z"}  # 17:00 in Harare
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")

    def close(meters, at="2026-10-01T19:10:00Z"):  # 21:10 in Harare: closes 1 Oct
        return mgr.post("/api/day-close/", {"station": str(fc.station.pk), "at": at, "dips": [{"tank": str(fc.tank.pk), "dip_mm": "1500", "litres": "5000"}],
                                            "meters": [{"nozzle": str(n.pk), "reading": str(v)} for n, v in meters]}, format="json")

    assert close(list(zip(fc.nozzles, [40, 0], strict=True))).status_code == 400  # Pump 2 missing
    no_mm = mgr.post("/api/day-close/", {"station": str(fc.station.pk), "at": "2026-10-01T19:10:00Z", "dips": [{"tank": str(fc.tank.pk), "litres": "5000"}],
                                         "meters": [{"nozzle": str(n.pk), "reading": "40"} for n in fc.nozzles + p2]}, format="json")
    assert no_mm.status_code == 400 and "dip height in mm first" in str(no_mm.json())  # the mm is always taken first
    assert close(list(zip(fc.nozzles + p2, [40, 0, 10, 0], strict=True))).status_code == 201
    again = close(list(zip(fc.nozzles + p2, [40, 0, 10, 0], strict=True)), at="2026-10-01T19:20:00Z")
    assert again.status_code == 400 and "already closed" in str(again.json())
    assert ca.post("/api/day-close/", {}, format="json").status_code == 403  # attendants never close the day
    lower = close(list(zip(fc.nozzles + p2, [30, 0, 10, 0], strict=True)), at="2026-10-02T19:05:00Z")
    assert lower.status_code == 400 and "below its last number" in str(lower.json())

    # A works on through the close and ends at 07:00: their part and cash are whole
    end = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": "2026-10-02T05:00:00Z", "readings": readings(fc.nozzles, [100, 0]), "cash": cash("148")}, format="json").json()
    assert end["status"] == "open"
    with db_context(tenant_id=a["tenant"].id):
        mine = Transaction.objects.filter(shift_id=sid, attendant=att_a).aggregate(s=Sum("litres"))["s"]
        assert mine == Decimal("100.000")  # 40 before the close + 60 after
    stage = ca.get("/api/shifts/prefill/").json()["stations"][0]["my_stage"]
    assert stage["expected"] == "150.00"  # 100 L at 1.50: the close did not change what they owe
    # the shift is still open (B works on): the day shows A's own turn (150 owed, 148 handed in); B is still working
    turn = mgr.get("/api/reports/daily/", {"station": str(fc.station.pk), "day": "2026-10-02"}).json()[0]["shifts"][0]
    assert (turn["attendants"], turn["expected"], turn["handed_in"], turn["cash_difference"], turn["waiting"]) == ([att_a.full_name], "150.00", "148.00", "-2.00", [])
    days = {d["date"]: d["litres"] for d in mgr.get("/api/reports/daily-sales/", {"date_from": "2026-10-01", "date_to": "2026-10-02"}).json()["days"]}
    assert days == {"2026-10-01": "50.000", "2026-10-02": "60.000"}  # 1 Oct: 40 (A) + 10 (B) to 21:10; 2 Oct: A's 60 after it
    # the daily report for 1 Oct: what the manager used to work out by hand at 21:00
    report = mgr.get("/api/reports/daily/", {"station": str(fc.station.pk), "day": "2026-10-01"}).json()[0]
    assert report["closed"] and (report["totals"]["litres"], report["totals"]["amount"]) == ("50.000", "75.00")
    meters = {(p["pump"], p["closing_meter"]) for p in report["pumps"] if p["closing_meter"] is not None}
    assert ("Pump 1", "40.00") in meters and ("Pump 2", "10.00") in meters
    assert {p["attendant"]: p["litres"] for p in report["people"]} == {att_a.full_name: "40.000", att_b.full_name: "10.000"}
    assert report["tanks"] == []  # the first dip ever: the fuel check runs from this close to the next
    assert [(f["fuel_code"], f["litres"]) for f in report["dips"]] == [(fc.tank.fuel_type.code, "5000.000")]  # the close dip
    # money for 1 Oct = the fuel sold (75.00). A handed in 148 for 150 of fuel, 60 of it before the close: 60 of their
    # cash settles 1 Oct. B has not handed in: their 15.00 before the close waits as "Value before split".
    money = lambda r: ([(h["label"], h["amount"]) for h in r["handed_in"] if h["amount"] != "0.00"], [(b["key"], b["amount"]) for b in r["balance"]])  # noqa: E731
    assert report["totals"]["grand_total"] == "75.00" and report["settled_at"] is None
    assert money(report) == ([("Cash", "60.00")], [("evening", "15.00")])

    # 2 Oct: A hands in 148 of 150 (a $2 loss is theirs); B hands in 14.50 of 15.00 (a 50c milking error)
    cb.post(f"/api/shifts/{sid}/end/", {"ended_at": "2026-10-02T05:10:00Z", "readings": readings(p2, [10, 0]),
                                        "cash": cash("10") + cash("4.50", kind="card")}, format="json")
    # in the morning B hands in 10 cash + 4.50 swipe for 15.00 of fuel, all before the close: it settles 1 Oct, from
    # cash first, then swipe (B is 0.50 short)
    settled = mgr.get("/api/reports/daily/", {"station": str(fc.station.pk), "day": "2026-10-01"}).json()[0]
    assert settled["totals"]["grand_total"] == "75.00" and settled["settled_at"] is not None
    assert money(settled) == ([("Cash", "70.00"), ("Swipe", "4.50")], [("short", "0.50")])
    report = mgr.get("/api/reports/daily/", {"station": str(fc.station.pk), "day": "2026-10-02"}).json()[0]
    # 2 Oct = A's 60 L after the close (90.00): A's 148 less the 60 that settled 1 Oct is 88 (A is 2.00 short)
    assert report["totals"]["grand_total"] == "90.00" == report["totals"]["amount"]
    assert money(report) == ([("Cash", "88.00")], [("short", "2.00")])
    losses = report["cash_losses"]
    assert (losses["total"], losses["milking"], losses["milking_count"]) == ("2.50", "0.50", 1)
    assert losses["by_attendant"] == [{"attendant": att_a.full_name, "amount": "2.00"}]
    # 2 Oct is not closed: Pump 1 opens at the 1 Oct close, and closes at the attendant's last reading (with its time)
    pump1 = next(p for p in report["pumps"] if p["pump"] == "Pump 1" and p["closing_meter"] == "100.00")
    assert pump1["opening_meter"] == "40.00" and pump1["opening_from"] == "close" and pump1["closing_from"] != "close"
    assert pump1["meter_litres"] == "60.000"


def test_an_earlier_day_can_be_closed_afterwards_and_its_sales_are_re_split():
    """The manager forgot to close 1 Oct. They enter its 21:00 dips and meters the next day: the night shift's sales
    are split at that moment between 1 Oct and 2 Oct; nobody's litres or money change."""
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": "2026-10-01T16:00:00Z"}  # 18:00 in Harare
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": "2026-10-02T05:00:00Z", "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150")}, format="json")
    cb.post(f"/api/shifts/{sid}/end/", {"ended_at": "2026-10-02T05:00:00Z", "readings": readings(p2, [5, 0]), "cash": cash("7.50")}, format="json")

    def sold():
        rows = mgr.get("/api/reports/daily-sales/", {"date_from": "2026-10-01", "date_to": "2026-10-02"}).json()["days"]
        return {d["date"]: d["litres"] for d in rows}

    assert sold() == {"2026-10-01": "0.000", "2026-10-02": "105.000"}  # no close: it all lands when the shift ended

    def close(meters, at="2026-10-01T19:00:00Z"):  # 21:00 on 1 Oct, entered on 2 Oct
        return mgr.post("/api/day-close/", {"station": str(fc.station.pk), "at": at, "dips": [{"tank": str(fc.tank.pk), "dip_mm": "1500", "litres": "5000"}],
                                            "meters": [{"nozzle": str(n.pk), "reading": str(v)} for n, v in zip(fc.nozzles + p2, meters, strict=True)]},
                        format="json")

    above = close([120, 0, 2, 0])
    assert above.status_code == 400 and "above the next number 100.00" in str(above.json())
    assert close([40, 0, 2, 0], at="2030-01-01T19:00:00Z").status_code == 400  # not in the future
    assert close([40, 0, 2, 0]).status_code == 201
    assert sold() == {"2026-10-01": "42.000", "2026-10-02": "63.000"}
    with db_context(tenant_id=a["tenant"].id):
        assert Transaction.objects.filter(shift_id=sid).aggregate(s=Sum("litres"))["s"] == Decimal("105.000")
        assert Transaction.objects.filter(shift_id=sid).aggregate(s=Sum("amount"))["s"] == Decimal("157.50")

    # Reports by day: newest first, each day with its shifts as the drill-down
    days = mgr.get("/api/reports/days/", {"date_from": "2026-10-01", "date_to": "2026-10-02", "station": str(fc.station.pk)}).json()["days"]
    assert [(d["day"], d["closed"], d["totals"]["litres"]) for d in days] == [("2026-10-02", False, "63.000"), ("2026-10-01", True, "42.000")]
    assert [s["shift_id"] for s in days[0]["shifts"]] == [sid] and days[0]["totals"]["grand_total"] == "94.50"  # = the fuel sold
    # 1 Oct, closed afterwards: both have handed in, so their 42 L before the close (63.00) is split from their cash
    assert days[1]["balance"] == [] and days[1]["totals"]["grand_total"] == "63.00" and days[1]["handed_in"][0]["amount"] == "63.00"


def test_the_manager_fuel_check_runs_from_close_meters_to_close_meters():
    """Close to close: opening dip + delivered − litres through the pumps (meters at the two closes) − closing dip.
    Pumping nobody recorded (before the attendants started using the system) is still counted."""
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()

    def close(at, dip, meters):
        r = mgr.post("/api/day-close/", {"station": str(fc.station.pk), "at": at, "dips": [{"tank": str(fc.tank.pk), "dip_mm": "1500", "litres": dip}],
                                         "meters": [{"nozzle": str(n.pk), "reading": str(v)} for n, v in zip(fc.nozzles + p2, meters, strict=True)]}, format="json")
        assert r.status_code == 201, r.content

    close("2026-10-01T19:00:00Z", "5000", [0, 0, 0, 0])
    close("2026-10-02T19:00:00Z", "4880", [100, 0, 10, 0])  # 110 L through the pumps, no attendant recorded any of it
    report = mgr.get("/api/reports/daily/", {"station": str(fc.station.pk), "day": "2026-10-02"}).json()[0]
    [check] = report["meter_checks"]
    assert (check["opening"], check["dispensed"], check["closing"], check["loss"], check["status"]) == ("5000.000", "110.000", "4880.000", "10.000", "ok")
    assert mgr.get("/api/reports/daily/", {"station": str(fc.station.pk), "day": "2026-10-01"}).json()[0]["meter_checks"] == []  # no close before it


def test_the_day_report_shows_day_and_night_shifts_apart_even_on_one_open_shift():
    """Assigned pumps keep one shift open while people come and go: the report still shows the night shift and the
    day shift as two lines, from when each turn was worked."""
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca, cb = client_for(att_a), client_for(att_b)
    station = str(fc.station.pk)
    sid = cb.post("/api/shifts/start/", {"station": station, "started_at": "2026-10-01T16:30:00Z", "readings": readings(p2, [0, 0])}, format="json").json()["id"]
    assert ca.post("/api/shifts/start/", {"station": station, "started_at": "2026-10-02T05:00:00Z", "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"] == sid
    cb.post(f"/api/shifts/{sid}/end/", {"ended_at": "2026-10-02T05:05:00Z", "readings": readings(p2, [10, 0]), "cash": cash("15")}, format="json")
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": "2026-10-02T16:00:00Z", "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150")}, format="json")
    shifts = mgr.get("/api/reports/daily/", {"station": station, "day": "2026-10-02"}).json()[0]["shifts"]
    assert [(s["shift_id"], s["label"], s["attendants"], s["expected"]) for s in shifts] == [
        (sid, "Night shift, Thu 01 Oct", [att_b.full_name], "15.00"), (sid, "Day shift, Fri 02 Oct", [att_a.full_name], "150.00")]


def test_cash_is_handed_in_in_whole_dollars():
    """Notes have no cents: cash with cents is refused (a typing slip, often the amount owed copied in). Swipe,
    EcoCash and intercompany credit can have cents."""
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": "2026-10-01T05:00:00Z", "readings": readings(fc.nozzles, [0, 0])},
                  format="json").json()["id"]
    end = {"ended_at": "2026-10-01T16:00:00Z", "readings": readings(fc.nozzles, [100, 0])}
    refused = ca.post(f"/api/shifts/{sid}/end/", {**end, "cash": cash("144.15") + cash("5.85", kind="mobile_money")}, format="json")
    assert refused.status_code == 400 and "Cash is counted in whole dollars" in str(refused.json())
    ok = ca.post(f"/api/shifts/{sid}/end/", {**end, "cash": cash("144") + cash("5.85", kind="mobile_money") + cash("0.15", kind="card")}, format="json")
    assert ok.status_code == 200, ok.content
