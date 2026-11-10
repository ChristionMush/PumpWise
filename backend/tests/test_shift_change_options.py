"""How the shift change works, per business (all off by default): attendants pick their own pumps when they start,
the outgoing attendant dips at the end of a shift, and the day closes by itself at a chosen shift change."""
import pytest

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.operations.models import DayClose, DayCloseReading, TankReading
from apps.operations.services import pump_gaps
from apps.stations.models import Pump, Station

from .helpers import client_for, platform_client
from .test_multi_attendant import assigned_site, cash, readings

# 1 Oct 2026 in Harare (UTC+2): day shift 07:00-18:00
DAY_START, A_ENDS, B_ENDS, NOON = "2026-10-01T05:10:00Z", "2026-10-01T15:55:00Z", "2026-10-01T16:05:00Z", "2026-10-01T10:00:00Z"


def options(a, **changes):
    res = platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/features/", changes, format="json")
    assert res.status_code == 200, res.content
    for k, v in changes.items():
        assert res.json()["features"][k] == v


def test_attendants_pick_free_pumps_when_they_start_and_the_manager_allocates_nothing():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()  # the manager had given Pump 1 to A and Pump 2 to B
    options(a, attendants_pick_pumps=True)
    ca, cb = client_for(att_a), client_for(att_b)
    entry = ca.get("/api/shifts/prefill/").json()["stations"][0]
    assert entry["pick_pumps"] is True and {p["name"] for p in entry["pumps"]} == {"Pump 1", "Pump 2"}  # every pump to choose from
    started = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "pumps": [str(pump2.pk)], "readings": readings(p2, [0, 0])}, format="json")
    assert started.status_code == 201, started.content
    with db_context(tenant_id=a["tenant"].id):
        assert Pump.objects.get(pk=pump2.pk).assigned_to_id == att_a.pk  # picked: theirs now
    taken = cb.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "pumps": [str(pump2.pk)], "readings": readings(p2, [0, 0])}, format="json")
    assert taken.status_code == 400 and "still with" in str(taken.json())
    ok = cb.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "pumps": [str(fc.pump.pk)], "readings": readings(fc.nozzles, [0, 0])}, format="json")
    assert ok.status_code == 201, ok.content
    sid = started.json()["id"]
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": A_ENDS, "readings": readings(p2, [10, 0]), "cash": cash("15")}, format="json")
    with db_context(tenant_id=a["tenant"].id):
        assert pump_gaps([Station.objects.get(pk=fc.station.pk)]) == []  # nothing for the manager to allocate


def test_the_outgoing_attendant_dips_and_the_day_closes_at_the_18_00_change():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    options(a, dip_at_shift_end=True, close_day_at_change="18:00")
    ca, cb = client_for(att_a), client_for(att_b)
    entry = ca.get("/api/shifts/prefill/").json()["stations"][0]
    assert entry["dip_at_end"] is True and [t["name"] for t in entry["tanks"]] == [fc.tank.name] and "last_litres" not in entry["tanks"][0]
    body = {"station": str(fc.station.pk), "started_at": DAY_START}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")

    # A ends first and dips; B is still on, so the day does not close yet
    ended = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": A_ENDS, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150"),
                                                 "dips": [{"tank": str(fc.tank.pk), "dip_mm": "500"}]}, format="json")
    assert ended.status_code == 200, ended.content
    with db_context(tenant_id=a["tenant"].id):
        dip = TankReading.objects.get(recorded_by=att_a)
        assert dip.shift_id is not None and str(dip.litres) == "5000.000"
        assert not DayClose.objects.exists()
    # B ends without dipping (A just did): everyone from the day shift has ended, so the day closes at B's end
    assert cb.post(f"/api/shifts/{sid}/end/", {"ended_at": B_ENDS, "readings": readings(p2, [40, 0]), "cash": cash("60")}, format="json").status_code == 200
    with db_context(tenant_id=a["tenant"].id):
        close = DayClose.objects.get()
        assert str(close.day) == "2026-10-01" and close.closed_by_id == att_b.pk and "evening shift change" in close.note
        assert TankReading.objects.get(day_close=close).pk == dip.pk
        assert sorted(str(r.reading) for r in DayCloseReading.objects.filter(day_close=close)) == ["0.00", "0.00", "100.00", "40.00"]


def test_an_end_away_from_the_closing_change_does_not_close_the_day():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    options(a, dip_at_shift_end=True, close_day_at_change="18:00")
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": NOON, "readings": readings(fc.nozzles, [10, 0]), "cash": cash("15"),
                                        "dips": [{"tank": str(fc.tank.pk), "dip_mm": "500"}]}, format="json")
    with db_context(tenant_id=a["tenant"].id):
        assert TankReading.objects.filter(recorded_by=att_a).count() == 1 and not DayClose.objects.exists()


@pytest.mark.parametrize("ends", ["2026-10-01T14:00:00Z", "2026-10-01T18:00:00Z"])  # 16:00 and 20:00 in Harare
def test_the_day_closes_whenever_the_evening_change_happens(ends):
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    options(a, dip_at_shift_end=True, close_day_at_change="18:00")
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    client_for(att_b).post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(p2, [0, 0])}, format="json")
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": ends, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150"),
                                        "dips": [{"tank": str(fc.tank.pk), "dip_mm": "500"}]}, format="json")
    client_for(att_b).post(f"/api/shifts/{sid}/end/", {"ended_at": ends, "readings": readings(p2, [40, 0]), "cash": cash("60")}, format="json")
    with db_context(tenant_id=a["tenant"].id):
        assert str(DayClose.objects.get().day) == "2026-10-01", ends


def test_a_dip_entered_after_the_shift_ended_closes_the_day():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    options(a, close_day_at_change="18:00")  # the attendant ends without dipping; the dip is entered afterwards
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    client_for(att_b).post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(p2, [0, 0])}, format="json")
    for c, r in ((ca, readings(fc.nozzles, [100, 0])), (client_for(att_b), readings(p2, [40, 0]))):
        c.post(f"/api/shifts/{sid}/end/", {"ended_at": "2026-10-01T15:00:00Z", "readings": r, "cash": cash("60")}, format="json")
    with db_context(tenant_id=a["tenant"].id):
        assert not DayClose.objects.exists()  # no dip yet
    dipped = mgr.post("/api/tank-readings/", {"tank": str(fc.tank.pk), "dip_mm": "500", "at": "2026-10-01T15:10:00Z"}, format="json")
    assert dipped.status_code == 201, dipped.content
    with db_context(tenant_id=a["tenant"].id):
        close = DayClose.objects.get()
        assert str(close.day) == "2026-10-01" and "17:10" in close.note


def test_without_the_option_an_attendant_cannot_send_dips_and_nothing_changes():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    ca = client_for(att_a)
    entry = ca.get("/api/shifts/prefill/").json()["stations"][0]
    assert "dip_at_end" not in entry and "tanks" not in entry and "pick_pumps" not in entry
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    refused = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": A_ENDS, "readings": readings(fc.nozzles, [10, 0]), "cash": cash("15"),
                                                  "dips": [{"tank": str(fc.tank.pk), "dip_mm": "500"}]}, format="json")
    assert refused.status_code == 403
    with db_context(tenant_id=a["tenant"].id):
        assert not TankReading.objects.filter(recorded_by=att_a).exists()
    assert client_for(a[Role.OWNER]).patch(f"/api/platform/tenants/{a['tenant'].id}/features/", {"dip_at_shift_end": True}, format="json").status_code == 403


def test_each_attendant_dips_only_the_tanks_of_the_pumps_they_are_on():
    from apps.stations.models import Nozzle

    from .helpers import second_tank

    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    tank2 = second_tank(fc)
    with db_context(tenant_id=a["tenant"].id):
        Nozzle.objects.filter(pump=pump2).update(tank=tank2)  # Pump 2 draws from Tank 2
    options(a, dip_at_shift_end=True)
    ca = client_for(att_a)
    nozzles = {n["id"]: n["tank"] for p in ca.get("/api/shifts/prefill/").json()["stations"][0]["pumps"] for n in p["nozzles"]}
    assert set(nozzles.values()) == {str(fc.tank.pk)}  # A's phone lists A's pump, and knows the tank each nozzle draws from
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    ended = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": A_ENDS, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150"),
                                                 "dips": [{"tank": str(fc.tank.pk), "dip_mm": "500"}, {"tank": str(tank2.pk), "dip_mm": "500", "litres": "4000"}]}, format="json")
    assert ended.status_code == 200, ended.content
    with db_context(tenant_id=a["tenant"].id):
        assert list(TankReading.objects.filter(recorded_by=att_a).values_list("tank_id", flat=True)) == [fc.tank.pk]  # Pump 1's tank only
    tanks = {t["name"]: t for t in ca.get("/api/shifts/prefill/").json()["stations"][0]["tanks"]}
    assert tanks[fc.tank.name]["last_dip_at"] is not None and tanks["Tank 2"]["last_dip_at"] is None and "last_litres" not in tanks["Tank 2"]


def test_an_attendant_adds_expenses_to_their_end_of_shift_and_their_cash_is_unchanged():
    import uuid

    from apps.operations.models import CashSubmission, Expense

    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    options(a, expenses_enabled=True)
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    lunch, fuel = str(uuid.uuid4()), str(uuid.uuid4())
    body = {"ended_at": A_ENDS, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150"),
            "expenses": [{"client_id": lunch, "name": "Lunch", "amount": "5"}, {"client_id": fuel, "name": "Generator fuel, 10 L", "amount": "14.50"}]}
    no_name = ca.post(f"/api/shifts/{sid}/end/", {**body, "expenses": [{"name": " ", "amount": "5"}]}, format="json")
    assert no_name.status_code == 400  # a name and a cost are both needed
    assert ca.post(f"/api/shifts/{sid}/end/", body, format="json").status_code == 200
    from apps.operations.models import Shift
    from apps.operations.services import record_shift_expenses

    with db_context(tenant_id=a["tenant"].id):  # the phone sending the same lines again records nothing twice
        record_shift_expenses(user=att_a, shift=Shift.objects.get(pk=sid), expenses=body["expenses"], at=Shift.objects.get(pk=sid).started_at)
    with db_context(tenant_id=a["tenant"].id):
        rows = list(Expense.objects.order_by("amount").values_list("note", "amount", "recorded_by_id", "shift_id"))
        assert [(n, str(m)) for n, m, _, _ in rows] == [("Lunch", "5.00"), ("Generator fuel, 10 L", "14.50")]
        assert {(u, s) for _, _, u, s in rows} == {(att_a.pk, uuid.UUID(sid))}
        assert list(CashSubmission.objects.filter(shift_id=sid).values_list("payment_type", "amount")) == [("cash", 150)]  # recorded only
    # the manager sees them on the end of shift being approved
    end = next(e for e in mgr.get("/api/shift-ends/").json() if e["person"] == att_a.full_name)
    detail = mgr.get(f"/api/shift-ends/{end['id']}/").json()
    assert [(e["name"], e["amount"]) for e in detail["expenses"]] == [("Lunch", "5.00"), ("Generator fuel, 10 L", "14.50")]


def test_an_attendant_dips_every_tank_their_nozzles_draw_from_both_petrol_tanks():
    from .helpers import second_tank

    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    backup = second_tank(fc, code="PETROL", name="Petrol Tank 2")
    diesel = second_tank(fc, code="DIESEL", name="Diesel Tank")
    for n in fc.nozzles:  # Setup: the petrol nozzles also draw from Petrol Tank 2
        assert client_for(a[Role.OWNER]).patch(f"/api/nozzles/{n.pk}/", {"also_draws_from": [str(backup.pk)]}, format="json").status_code == 200
    options(a, dip_at_shift_end=True)
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    dips = [{"tank": str(fc.tank.pk), "dip_mm": "500"}, {"tank": str(backup.pk), "dip_mm": "300", "litres": "2500"},
            {"tank": str(diesel.pk), "dip_mm": "300", "litres": "4000"}]
    ended = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": A_ENDS, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150"), "dips": dips}, format="json")
    assert ended.status_code == 200, ended.content
    with db_context(tenant_id=a["tenant"].id):
        dipped = set(TankReading.objects.filter(recorded_by=att_a).values_list("tank__name", flat=True))
    assert dipped == {fc.tank.name, "Petrol Tank 2"}  # both petrol tanks; the diesel tank is not on their pump


def test_expenses_handed_in_with_the_cash_after_the_meters_belong_to_that_shift():
    import uuid

    from apps.operations.models import Expense

    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    options(a, expenses_enabled=True)
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    # the shift change: meters closed first, the cash counted and handed in after
    assert ca.post(f"/api/shifts/{sid}/end/", {"ended_at": A_ENDS, "readings": readings(fc.nozzles, [100, 0]), "cash": [], "cash_later": True}, format="json").status_code == 200
    handed = ca.post(f"/api/shifts/{sid}/cash/", {"cash": cash("150"), "expenses": [{"client_id": str(uuid.uuid4()), "name": "Lunch", "amount": "5"}]}, format="json")
    assert handed.status_code == 200, handed.content
    with db_context(tenant_id=a["tenant"].id):
        assert str(Expense.objects.get().shift_id) == sid  # on the shift it was paid on, though the part had ended
    assert client_for(a[Role.OWNER]).get("/api/expenses/", {"date_from": "10 Nov"}).status_code == 400  # not a date


def test_an_offline_end_with_a_dip_that_cannot_be_right_is_kept_and_the_dip_set_aside():

    from apps.operations.models import Shift

    from .helpers import second_tank
    from .test_multi_attendant import op

    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    tank2 = second_tank(fc, code="PETROL", name="Petrol Tank 2")  # holds 10 000 L
    for n in fc.nozzles:
        client_for(a[Role.OWNER]).patch(f"/api/nozzles/{n.pk}/", {"also_draws_from": [str(tank2.pk)]}, format="json")
    options(a, dip_at_shift_end=True)
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    dips = [{"tank": str(fc.tank.pk), "dip_mm": "500"}, {"tank": str(tank2.pk), "dip_mm": "1700", "litres": "20000"}]  # more than Tank 2 holds
    body = {"shift_id": sid, "ended_at": A_ENDS, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150"), "dips": dips}
    online = ca.post(f"/api/shifts/{sid}/end/", {k: v for k, v in body.items() if k != "shift_id"}, format="json")
    assert online.status_code == 400 and "more than the tank can hold" in str(online.json())  # online: corrected on the spot
    synced = ca.post("/api/sync/", {"ops": [op("shift_end", body)]}, format="json")
    assert synced.json()["results"][0]["status"] == "applied", synced.content  # offline: the end is kept
    with db_context(tenant_id=a["tenant"].id):
        assert set(TankReading.objects.filter(recorded_by=att_a).values_list("tank__name", flat=True)) == {fc.tank.name}  # the good dip saved
        assert f"dip_refused:{tank2.pk}" in Shift.objects.get(pk=sid).flags
    end = next(e for e in mgr.get("/api/shift-ends/").json() if e["shift_id"] == sid)
    assert "Petrol Tank 2: the dip (1700 mm, 20000 L) was set aside: That is more than the tank can hold. Check the number. Dip it again." in end["checks"], end["checks"]

