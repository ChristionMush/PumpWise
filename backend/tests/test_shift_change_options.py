"""How the shift change works, per business (all off by default): attendants pick their own pumps when they start,
the outgoing attendant dips at the end of a shift, and the day closes by itself at a chosen shift change."""
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
        assert str(close.day) == "2026-10-01" and close.closed_by_id == att_b.pk and "18:00 shift change" in close.note
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
