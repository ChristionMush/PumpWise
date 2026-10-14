"""Phase 3-4: the shift flow, price-at-time, and the DataSourceAdapter seam."""
from decimal import Decimal

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.operations.models import PumpMeterReading, Transaction

from .helpers import at, client_for, make_forecourt, run_shift, two_tenants


def body(fc, readings, **extra):
    return {"station": str(fc.station.pk), "readings": [{"nozzle": str(n.pk), "reading": str(r)} for n, r in zip(fc.nozzles, readings, strict=False)], **extra}


def test_full_shift_over_the_api_calculates_expected_and_compares_with_whats_handed_in():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    att = client_for(a[Role.ATTENDANT])
    started = att.post("/api/shifts/start/", body(fc, [1000, 5000]), format="json")
    assert started.status_code == 201, started.content
    sid = started.json()["id"]
    end = {"readings": [{"nozzle": str(fc.nozzles[0].pk), "reading": "1100"}, {"nozzle": str(fc.nozzles[1].pk), "reading": "5040"}],
           "cash": [{"payment_type": "cash", "currency": "USD", "amount": "150.00"}, {"payment_type": "mobile_money", "currency": "USD", "amount": "60"}]}
    res = att.post(f"/api/shifts/{sid}/end/", end, format="json")
    assert res.status_code == 200, res.content
    d = res.json()
    # (100 + 40) litres x 1.50 = 210.00 expected; 150 + 60 handed in
    assert d["status"] == "closed" and d["summary"]["expected"] == "210.00" and d["summary"]["submitted_total"] == "210.00"
    assert d["summary"]["status"] == "ok" and d["summary"]["flagged"] is False
    assert sorted(Decimal(t["litres"]) for t in d["transactions"]) == [Decimal("40"), Decimal("100")]
    assert all(t["price_per_litre"] == "1.5000" and t["currency"] == "USD" for t in d["transactions"])


def test_cash_shortage_is_flagged_by_payment_type():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="2.00")
    shift = run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14),
                      cash=[("cash", "USD", 120), ("mobile_money", "USD", 30)])
    d = client_for(a[Role.OWNER]).get(f"/api/shifts/{shift.pk}/").json()["summary"]
    assert d["expected"] == "200.00" and d["variance"] == "-50.00" and d["status"] == "short" and d["flagged"] is True
    assert {(x["payment_type"], x["amount"]) for x in d["by_payment_type"]} == {("cash", "120.00"), ("mobile_money", "30.00")}


def test_foreign_currency_handed_in_is_converted_with_the_owners_rate():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="2.00")
    c = client_for(a[Role.OWNER])
    run = lambda: run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14), cash=[("cash", "USD", 100), ("cash", "ZWG", 2700)])  # noqa: E731
    shift = run()
    assert c.get(f"/api/shifts/{shift.pk}/").json()["summary"]["status"] == "incomplete"  # no rate yet: flagged, not guessed
    c.post("/api/exchange-rates/", {"from_currency": "ZWG", "to_currency": "USD", "rate": "0.037", "effective_from": "2026-09-01T00:00:00Z"}, format="json")
    s = c.get(f"/api/shifts/{shift.pk}/").json()["summary"]
    assert s["submitted_total"] == "199.90" and s["status"] == "ok" and s["missing_rate"] == []  # 100 + 2700 x 0.037


def test_price_change_during_a_shift_uses_a_checkpoint_so_each_part_has_the_right_price():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00", price_from=at(2026, 9, 1))
    c = client_for(a[Role.OWNER])
    c.post("/api/prices/", {"station": str(fc.station.pk), "fuel_type": str(fc.fuel.pk), "price_per_litre": "1.20", "effective_from": "2026-10-01T10:00:00Z"}, format="json")
    shift = run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14), checkpoint=(at(2026, 10, 1, 10), [50, 0]))
    txs = c.get(f"/api/shifts/{shift.pk}/").json()["transactions"]
    busy = sorted((t for t in txs if Decimal(t["litres"]) > 0), key=lambda t: t["started_at"])
    assert [(t["litres"], t["price_per_litre"], t["amount"]) for t in busy] == [("50.000", "1.0000", "50.00"), ("50.000", "1.2000", "60.00")]


def test_meter_rollover_backwards_numbers_and_gaps_are_flagged_not_refused():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00")
    s1 = run_shift(a["tenant"], a[Role.ATTENDANT], fc, [99999990, 100], [10, 90], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14))
    with db_context(tenant_id=a["tenant"].id):
        by_nozzle = {t.nozzle_id: t for t in Transaction.objects.filter(shift=s1)}
        assert by_nozzle[fc.nozzles[0].pk].litres == Decimal("20.00")  # rolled over: 99999990 -> 10
        assert by_nozzle[fc.nozzles[1].pk].litres == 0  # went backwards: not invented
        s1.refresh_from_db()
        assert any(f.startswith("meter_rollover") for f in s1.flags) and any(f.startswith("meter_went_backwards") for f in s1.flags)
    # next shift starts at a different number than the last end -> gap flagged on the reading
    s2 = run_shift(a["tenant"], a[Role.ATTENDANT], fc, [15, 90], [30, 120], started_at=at(2026, 10, 2, 6), ended_at=at(2026, 10, 2, 14))
    with db_context(tenant_id=a["tenant"].id):
        r = PumpMeterReading.objects.get(shift=s2, nozzle=fc.nozzles[0], kind="start")
        assert r.flags == ["gap_since_last_shift:5.00"]
        s2.refresh_from_db()
        assert any(f.startswith("gap_since_last_shift") for f in s2.flags)


def test_no_price_is_flagged_and_litres_are_still_counted():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price=None)
    shift = run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [10, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14))
    d = client_for(a[Role.OWNER]).get(f"/api/shifts/{shift.pk}/").json()
    assert any(f.startswith("no_price") for f in d["flags"])
    assert sum(Decimal(t["litres"]) for t in d["transactions"]) == 10


def test_ending_requires_every_started_pump_over_the_api():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    att = client_for(a[Role.ATTENDANT])
    sid = att.post("/api/shifts/start/", body(fc, [0, 0]), format="json").json()["id"]
    res = att.post(f"/api/shifts/{sid}/end/", {"readings": [{"nozzle": str(fc.nozzles[0].pk), "reading": "5"}]}, format="json")
    assert res.status_code == 400 and "1 missing" in str(res.json())


def test_correction_before_review_replaces_the_number_and_after_review_is_refused():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00")
    shift = run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14))
    att, mgr = client_for(a[Role.ATTENDANT]), client_for(a[Role.MANAGER])
    fix = {"readings": [{"nozzle": str(fc.nozzles[0].pk), "reading": "90"}, {"nozzle": str(fc.nozzles[1].pk), "reading": "0"}]}
    d = att.post(f"/api/shifts/{shift.pk}/end/", fix, format="json").json()
    assert d["summary"]["expected"] == "90.00" and any(r["corrected"] for r in d["readings"])
    with db_context(tenant_id=a["tenant"].id):
        assert Transaction.all_objects.filter(shift=shift, deleted_at__isnull=False).count() == 2  # old rows kept, soft-deleted
        assert Transaction.objects.filter(shift=shift).count() == 2
    assert att.post(f"/api/shifts/{shift.pk}/review/", {}, format="json").status_code == 403
    assert mgr.post(f"/api/shifts/{shift.pk}/review/", {"notes": "checked"}, format="json").json()["status"] == "reviewed"
    assert att.post(f"/api/shifts/{shift.pk}/end/", fix, format="json").status_code == 400
    with db_context(tenant_id=a["tenant"].id):
        assert AuditLog.objects.filter(action="shift_reviewed").count() == 1


def test_attendants_only_touch_their_own_shifts_but_managers_can_help():
    a, _ = two_tenants()
    from .helpers import make_user

    fc = make_forecourt(a["tenant"])
    other = make_user(a["tenant"], Role.ATTENDANT)
    sid = client_for(other).post("/api/shifts/start/", body(fc, [0, 0]), format="json").json()["id"]
    mine = client_for(a[Role.ATTENDANT])
    end = {"readings": [{"nozzle": str(n.pk), "reading": "1"} for n in fc.nozzles]}
    assert mine.get(f"/api/shifts/{sid}/").status_code == 404
    assert mine.post(f"/api/shifts/{sid}/end/", end, format="json").status_code == 404
    assert client_for(a[Role.MANAGER]).post(f"/api/shifts/{sid}/end/", end, format="json").status_code == 200
    assert mine.get("/api/shifts/").json()["count"] == 0
    assert client_for(a[Role.MANAGER]).get("/api/shifts/").json()["count"] == 1


def test_start_is_idempotent_on_the_phones_shift_id_and_rejects_foreign_pumps():
    a, b = two_tenants()
    fa, fb = make_forecourt(a["tenant"]), make_forecourt(b["tenant"])
    att = client_for(a[Role.ATTENDANT])
    sid = "11111111-1111-4111-8111-111111111111"
    first = att.post("/api/shifts/start/", body(fa, [0, 0], shift_id=sid), format="json")
    again = att.post("/api/shifts/start/", body(fa, [0, 0], shift_id=sid), format="json")
    assert first.status_code == again.status_code == 201 and first.json()["id"] == again.json()["id"] == sid
    assert att.get("/api/shifts/").json()["count"] == 1
    assert att.post("/api/shifts/start/", body(fb, [0, 0]), format="json").status_code == 400  # B's station is invisible
    # a shift id already used in another tenant can never be taken over
    assert client_for(b[Role.ATTENDANT]).post("/api/shifts/start/", body(fb, [0, 0], shift_id=sid), format="json").status_code == 400


def test_automatic_data_sources_have_no_adapter_yet_so_manual_entry_is_refused_with_a_clear_message():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    owner = client_for(a[Role.OWNER])
    owner.patch(f"/api/pumps/{fc.pump.pk}/", {"data_source": "pulse_reader"}, format="json")
    res = client_for(a[Role.ATTENDANT]).post("/api/shifts/start/", body(fc, [0, 0]), format="json")
    assert res.status_code == 400 and "not enabled yet" in str(res.json())
    owner.patch(f"/api/pumps/{fc.pump.pk}/", {"data_source": "manual"}, format="json")
    assert client_for(a[Role.ATTENDANT]).post("/api/shifts/start/", body(fc, [0, 0]), format="json").status_code == 201


def test_adapter_registry_dispatches_by_data_source_and_new_adapters_plug_in():
    import pytest

    from apps.operations import adapters

    assert isinstance(adapters.get_adapter("manual"), adapters.ManualAdapter)
    with pytest.raises(adapters.AdapterNotAvailable):
        adapters.get_adapter("controller")

    class Fake(adapters.DataSourceAdapter):
        source = "controller"
        def record_meter_reading(self, **kw): return "meter"
        def record_tank_reading(self, **kw): return "tank"

    adapters.register("controller", Fake())
    try:
        assert adapters.get_adapter("controller").record_meter_reading() == "meter"
    finally:
        adapters._REGISTRY.pop("controller")


def test_prefill_gives_the_phone_everything_for_offline_work():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [10, 20], [110, 70], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14))
    res = client_for(a[Role.ATTENDANT]).get("/api/shifts/prefill/").json()
    st = res["stations"][0]
    assert st["name"] == "Main Station" and st["pricing_currency"] == "USD" and st["open_shift_id"] is None
    nozzles = {n["number"]: n["last_reading"] for n in st["pumps"][0]["nozzles"]}
    assert nozzles == {1: "110.00", 2: "70.00"}  # next shift starts prefilled with the last end number
    assert "tanks" not in st and "qr_token" not in st["pumps"][0]  # attendants never get tank data (rule 11); QR is off
    assert {n["fuel_code"] for n in st["pumps"][0]["nozzles"]} == {"PETROL"}  # each nozzle carries its own fuel
    assert st["prices"][0]["price"] == "1.5000"
    mgr = client_for(a[Role.MANAGER]).get("/api/shifts/prefill/").json()["stations"][0]
    assert mgr["tanks"][0]["capacity_litres"] == "10000.000"
