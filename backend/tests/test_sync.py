"""Offline sync: idempotent, order-preserving, never loses data, never blocks on odd numbers."""
import uuid
from decimal import Decimal

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.operations.models import Delivery, Shift, SyncOperation, TankReading, Transaction

from .helpers import client_for, make_forecourt, make_user, two_tenants


def op(kind, payload, op_id=None):
    return {"op_id": op_id or str(uuid.uuid4()), "kind": kind, "payload": payload}


def shift_ops(fc, shift_id, start=(0, 0), end=(100, 50), cash=None):
    n = [str(x.pk) for x in fc.nozzles]
    return [
        op("shift_start", {"shift_id": shift_id, "station": str(fc.station.pk), "started_at": "2026-10-01T06:00:00Z", "readings": [{"nozzle": n[0], "reading": str(start[0])}, {"nozzle": n[1], "reading": str(start[1])}]}),
        op("shift_end", {"shift_id": shift_id, "ended_at": "2026-10-01T14:00:00Z", "readings": [{"nozzle": n[0], "reading": str(end[0])}, {"nozzle": n[1], "reading": str(end[1])}],
                         "cash": cash or [{"payment_type": "cash", "currency": "USD", "amount": "225"}]}),
    ]


def tank_ops(fc, sid=None):
    return [
        op("tank_dip", {"client_id": str(uuid.uuid4()), "tank": str(fc.tank.pk), "litres": "4000", "at": "2026-10-01T06:05:00Z", "shift": sid}),
        op("delivery", {"client_id": str(uuid.uuid4()), "tank": str(fc.tank.pk), "litres": "3000", "delivered_at": "2026-10-01T09:00:00Z", "supplier": "Zuva", "note_number": "N-1"}),
    ]


def test_a_whole_offline_shift_syncs_in_one_batch():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    sid = str(uuid.uuid4())
    res = client_for(a[Role.ATTENDANT]).post("/api/sync/", {"ops": shift_ops(fc, sid)}, format="json")
    assert res.status_code == 200, res.content
    assert [r["status"] for r in res.json()["results"]] == ["applied"] * 2
    # the manager's phone syncs the dip and the delivery (attendants never record these)
    res = client_for(a[Role.MANAGER]).post("/api/sync/", {"ops": tank_ops(fc, sid)}, format="json")
    assert [r["status"] for r in res.json()["results"]] == ["applied"] * 2
    with db_context(tenant_id=a["tenant"].id):
        shift = Shift.objects.get(pk=sid)
        assert shift.status == "closed"
        assert sum(t.amount for t in Transaction.objects.filter(shift=shift)) == Decimal("225.00")  # 150 litres x 1.50
        assert TankReading.objects.count() == 1 and Delivery.objects.get().litres_received == 3000
        assert TankReading.objects.get().recorded_by_id == a[Role.MANAGER].pk


def test_attendants_cannot_sync_dips_or_deliveries():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    res = client_for(a[Role.ATTENDANT]).post("/api/sync/", {"ops": tank_ops(fc)}, format="json").json()["results"]
    assert [r["status"] for r in res] == ["rejected", "rejected"] and all("manager" in r["error"] for r in res)
    with db_context(tenant_id=a["tenant"].id):
        assert TankReading.objects.count() == 0 and Delivery.objects.count() == 0


def test_dips_and_deliveries_are_idempotent_on_the_phones_client_id():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    ops = tank_ops(fc)
    mgr = client_for(a[Role.MANAGER])
    mgr.post("/api/sync/", {"ops": ops}, format="json")
    # same records, new op ids (e.g. the phone lost the first response and rebuilt its queue): still no duplicates
    again = [{**o, "op_id": str(uuid.uuid4())} for o in ops]
    assert [r["status"] for r in mgr.post("/api/sync/", {"ops": again}, format="json").json()["results"]] == ["applied", "applied"]
    with db_context(tenant_id=a["tenant"].id):
        assert TankReading.objects.count() == 1 and Delivery.objects.count() == 1


def test_resending_the_same_queue_changes_nothing():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    ops = shift_ops(fc, str(uuid.uuid4()))
    c = client_for(a[Role.ATTENDANT])
    first = c.post("/api/sync/", {"ops": ops}, format="json").json()["results"]
    again = c.post("/api/sync/", {"ops": ops}, format="json").json()["results"]
    assert [r["status"] for r in again] == ["applied", "applied"] and all(r["duplicate"] for r in again)
    assert [r.get("result") for r in again] == [r.get("result") for r in first]
    with db_context(tenant_id=a["tenant"].id):
        assert Shift.objects.count() == 1 and Transaction.objects.count() == 2 and SyncOperation.objects.count() == 2


def test_one_bad_entry_does_not_stop_the_rest_and_is_kept_for_a_manager():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    good = shift_ops(fc, str(uuid.uuid4()))
    bad = op("shift_end", {"shift_id": str(uuid.uuid4()), "readings": [], "cash": []})  # shift unknown
    junk = {"op_id": "not-a-uuid", "kind": "shift_start", "payload": {}}
    res = client_for(a[Role.ATTENDANT]).post("/api/sync/", {"ops": [bad, *good, junk, op("mystery", {})]}, format="json").json()["results"]
    assert [r["status"] for r in res] == ["rejected", "applied", "applied", "rejected", "rejected"]
    rejected = client_for(a[Role.MANAGER]).get("/api/sync/rejected/").json()
    assert {r["kind"] for r in rejected} == {"shift_end", "mystery"} and rejected[0]["user"]
    assert client_for(a[Role.ATTENDANT]).get("/api/sync/rejected/").status_code == 403


def test_odd_numbers_are_saved_and_flagged_never_refused():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    sid = str(uuid.uuid4())
    res = client_for(a[Role.ATTENDANT]).post("/api/sync/", {"ops": shift_ops(fc, sid, start=(500, 0), end=(100, 0))}, format="json").json()["results"]
    assert [r["status"] for r in res] == ["applied", "applied"]
    with db_context(tenant_id=a["tenant"].id):
        assert any(f.startswith("meter_went_backwards") for f in Shift.objects.get(pk=sid).flags)


def test_a_correction_syncs_until_a_manager_reviews_then_is_rejected():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00")
    sid = str(uuid.uuid4())
    c = client_for(a[Role.ATTENDANT])
    c.post("/api/sync/", {"ops": shift_ops(fc, sid, end=(100, 0))}, format="json")
    fix = [shift_ops(fc, sid, end=(80, 0))[1]]
    assert c.post("/api/sync/", {"ops": fix}, format="json").json()["results"][0]["status"] == "applied"
    with db_context(tenant_id=a["tenant"].id):
        assert sum(t.amount for t in Transaction.objects.filter(shift_id=sid)) == Decimal("80.00")
    client_for(a[Role.MANAGER]).post(f"/api/shifts/{sid}/review/", {}, format="json")
    late = [shift_ops(fc, sid, end=(60, 0))[1]]
    r = c.post("/api/sync/", {"ops": late}, format="json").json()["results"][0]
    assert r["status"] == "rejected" and "reviewed" in r["error"]


def test_attendants_cannot_sync_into_someone_elses_shift_or_another_business():
    a, b = two_tenants()
    fa, fb = make_forecourt(a["tenant"]), make_forecourt(b["tenant"])
    other = make_user(a["tenant"], Role.ATTENDANT)
    sid = str(uuid.uuid4())
    client_for(other).post("/api/sync/", {"ops": shift_ops(fa, sid)[:1]}, format="json")
    mine = client_for(a[Role.ATTENDANT]).post("/api/sync/", {"ops": [shift_ops(fa, sid)[1]]}, format="json").json()["results"][0]
    assert mine["status"] == "rejected" and "not your shift" in mine["error"]
    # tenant B referencing A's ids: every id is invisible
    foreign = client_for(b[Role.ATTENDANT]).post("/api/sync/", {"ops": shift_ops(fa, str(uuid.uuid4()))}, format="json").json()["results"]
    assert [r["status"] for r in foreign] == ["rejected", "rejected"]
    with db_context(tenant_id=b["tenant"].id):
        assert Shift.objects.count() == 0 and SyncOperation.objects.filter(status="applied").count() == 0
    del fb


def test_batch_size_is_limited_and_op_ids_are_per_business():
    a, b = two_tenants()
    fa, fb = make_forecourt(a["tenant"]), make_forecourt(b["tenant"])
    assert client_for(a[Role.ATTENDANT]).post("/api/sync/", {"ops": [{}] * 201}, format="json").status_code == 400
    assert client_for(a[Role.ATTENDANT]).post("/api/sync/", {"ops": "nope"}, format="json").status_code == 400
    same = str(uuid.uuid4())
    ra = client_for(a[Role.ATTENDANT]).post("/api/sync/", {"ops": [op("shift_start", shift_ops(fa, str(uuid.uuid4()))[0]["payload"], same)]}, format="json").json()["results"][0]
    rb = client_for(b[Role.ATTENDANT]).post("/api/sync/", {"ops": [op("shift_start", shift_ops(fb, str(uuid.uuid4()))[0]["payload"], same)]}, format="json").json()["results"][0]
    assert ra["status"] == rb["status"] == "applied" and not rb.get("duplicate")  # B is not told A used this id
