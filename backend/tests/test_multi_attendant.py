"""Several attendants on one shift (PLAN.md section 4): joining, leaving, per-person cash, closing, readings that differ,
assigned pumps with a mid-shift swap, and two phones syncing the same shift offline."""
import uuid
from decimal import Decimal

from django.db import connections, transaction
from django.db.migrations.executor import MigrationExecutor

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
    done = cb.post(f"/api/shifts/{sid}/end/", {"readings": readings(fc.nozzles, [100, 0]), "cash": cash("150")}, format="json").json()
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
    d = ca.post(f"/api/shifts/{sid}/end/", {"readings": readings(fc.nozzles, [90, 0]), "cash": cash("90")}, format="json").json()
    assert d["summary"]["expected"] == "90.00" and d["summary"]["submitted_total"] == "90.00"
    with db_context(tenant_id=a["tenant"].id):
        old = PumpMeterReading.all_objects.get(shift_id=sid, nozzle=fc.nozzles[0], kind="end", deleted_at__isnull=False)
        assert old.reading == Decimal("100.00")
        assert AuditLog.objects.filter(action="cash_corrected").exists()


# ---- assigned pumps ----------------------------------------------------------------------------------------------

def test_assigned_pumps_need_every_pump_assigned_and_a_swap_splits_the_litres_at_the_meter_snapshot():
    a, fc, att_a, att_b = setup("assigned")
    with db_context(tenant_id=a["tenant"].id):
        pump2 = Pump.objects.create(tenant_id=a["tenant"].id, station=fc.station, tank=fc.tank, name="Pump 2")
        p2n = Nozzle.objects.create(tenant_id=a["tenant"].id, pump=pump2, number=1)
    nozzles = [*fc.nozzles, p2n]
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": T0, "readings": readings(nozzles, [0, 0, 0])}
    missing = ca.post("/api/shifts/start/", body, format="json")
    assert missing.status_code == 400 and "Pump 2" in str(missing.json()) and "Pump 1" in str(missing.json())
    started = ca.post("/api/shifts/start/", {**body, "assignments": {str(fc.pump.pk): str(att_a.pk), str(pump2.pk): str(att_b.pk)}}, format="json")
    assert started.status_code == 201, started.content
    sid = started.json()["id"]
    assert {p["name"] for p in started.json()["attendants"]} == {att_a.full_name, "Second Attendant"}  # B was added by the assignment
    swap = {"pump": str(fc.pump.pk), "user": str(att_b.pk), "at": T_SWAP, "readings": readings(fc.nozzles, [50, 0])}
    assert cb.post(f"/api/shifts/{sid}/reassign/", swap, format="json").status_code == 403  # not the lead
    assert ca.post(f"/api/shifts/{sid}/reassign/", {**swap, "readings": readings(fc.nozzles[:1], [50])}, format="json").status_code == 400  # every nozzle
    assert ca.post(f"/api/shifts/{sid}/reassign/", swap, format="json").status_code == 200
    # B: 50 L on pump 1 after the swap + 20 L on pump 2 = 70 L x 1.50 = 105; A: 50 L before the swap = 75
    cb.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": [], "cash": cash("105")}, format="json")
    d = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(nozzles, [100, 0, 20]), "cash": cash("70")}, format="json").json()
    assert d["status"] == "closed", d
    s = d["summary"]
    assert s["per_attendant_expected"] is True
    people = {p["name"]: p for p in s["by_attendant"]}
    assert (people[att_a.full_name]["expected"], people[att_a.full_name]["cash_difference"]) == ("75.00", "-5.00")
    assert (people["Second Attendant"]["expected"], people["Second Attendant"]["cash_difference"]) == ("105.00", "0.00")
    assert {(p["pump"], p["to"] is not None) for p in people[att_a.full_name]["periods"]} == {("Pump 1", True)}
    assert {p["pump"] for p in people["Second Attendant"]["periods"]} == {"Pump 1", "Pump 2"}
    with db_context(tenant_id=a["tenant"].id):
        holders = {(t.nozzle_id, t.started_at.hour): t.attendant_id for t in Transaction.objects.filter(shift_id=sid) if t.litres > 0}
        assert holders == {(fc.nozzles[0].pk, 6): att_a.pk, (fc.nozzles[0].pk, 10): att_b.pk, (p2n.pk, 6): att_b.pk}
        assert AuditLog.objects.filter(action="pump_reassigned").count() == 1


def test_prefill_offers_the_open_shift_to_join_and_the_last_assignment_to_reuse():
    a, fc, att_a, att_b = setup("assigned")
    ca = client_for(att_a)
    assignments = {str(fc.pump.pk): str(att_a.pk)}
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "readings": readings(fc.nozzles, [0, 0]), "assignments": assignments}, format="json").json()["id"]
    st = client_for(att_b).get("/api/shifts/prefill/").json()["stations"][0]
    assert st["attendants_mode"] == "multiple" and st["assignment_mode"] == "assigned"
    assert st["joinable_shift"]["id"] == sid and st["open_shift_id"] is None
    assert {s["name"] for s in st["staff"]} >= {att_a.full_name, "Second Attendant"}
    assert st["previous_assignments"] == assignments
    assert client_for(att_a).get("/api/shifts/prefill/").json()["stations"][0]["open_shift_id"] == sid


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
