"""A business that closes the day at the shift change (PESNAT): the day runs from one handover to the next, a turn
belongs whole to the day it ended in, and the dashboard says when a day was not closed."""
from datetime import date, datetime
from datetime import timezone as tz

from apps.core.db import db_context
from apps.operations import dayclose, tradingday
from apps.operations.models import DayClose
from apps.stations.models import Station

from .helpers import client_for
from .test_multi_attendant import assigned_site, cash, readings
from .test_shift_change_options import DAY_START, options

HANDOVER = "2026-10-01T14:29:00Z"  # 16:29 in Harare


def _local(*args):
    return datetime(*args, tzinfo=tz.utc)


def test_an_unclosed_day_ends_at_the_shift_change_not_at_21_00():
    a, fc, *_ = assigned_site()
    with db_context(tenant_id=a["tenant"].id):
        assert tradingday.bounds(fc.station.pk, date(2026, 10, 2)) == (_local(2026, 10, 1, 19), _local(2026, 10, 2, 19))  # 21:00 Harare
    options(a, close_day_at_change="18:00")
    with db_context(tenant_id=a["tenant"].id):
        assert tradingday.bounds(fc.station.pk, date(2026, 10, 2)) == (_local(2026, 10, 1, 16), _local(2026, 10, 2, 16))  # 18:00 Harare


def test_the_day_runs_handover_to_handover_and_money_handed_in_after_it_stays_on_that_day():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    options(a, dip_at_shift_end=True, close_day_at_change="18:00")
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": DAY_START}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    # A closes the meters and dips at the handover, the cash comes later; B ends with the cash
    assert ca.post(f"/api/shifts/{sid}/end/", {"ended_at": HANDOVER, "readings": readings(fc.nozzles, [100, 0]), "cash": [], "cash_later": True,
                                               "dips": [{"tank": str(fc.tank.pk), "dip_mm": "500"}]}, format="json").status_code == 200
    assert cb.post(f"/api/shifts/{sid}/end/", {"ended_at": HANDOVER, "readings": readings(p2, [40, 0]), "cash": cash("60")}, format="json").status_code == 200
    with db_context(tenant_id=a["tenant"].id):
        assert DayClose.objects.get().closed_at == _local(2026, 10, 1, 14, 29)

    day = mgr.get("/api/reports/daily/", {"station": str(fc.station.pk), "day": "2026-10-01"}).json()[0]
    assert day["to"].startswith("2026-10-01T16:29")
    assert [(b["key"], b["label"]) for b in day["balance"] if b["key"] != "short"] == [("unsent", "Not handed in yet")]
    assert [w["name"] for s in day["shifts"] for w in s["waiting"]] == [att_a.full_name]

    # A hands in after the close, part of it as intercompany credit: all of it is that day's, none of it the next day's
    credit = [*cash("100"), {"payment_type": "credit", "currency": "USD", "amount": "50", "note": "Zuva Petroleum, ABC 1234"}]
    assert ca.post(f"/api/shifts/{sid}/cash/", {"cash": credit}, format="json").status_code == 200
    day = mgr.get("/api/reports/daily/", {"station": str(fc.station.pk), "day": "2026-10-01"}).json()[0]
    handed = {h["payment_type"]: h["amount"] for h in day["handed_in"]}
    assert handed["credit"] == "50.00" and handed["cash"] == "160.00"
    assert [b["key"] for b in day["balance"] if b["key"] != "short"] == []
    assert sorted(p["name"] for s in day["shifts"] for p in s["people"]) == sorted([att_a.full_name, att_b.full_name])
    nxt = mgr.get("/api/reports/daily/", {"station": str(fc.station.pk), "day": "2026-10-02"}).json()[0]
    assert nxt["from"].startswith("2026-10-01T16:29") and all(h["amount"] == "0.00" for h in nxt["handed_in"]) and nxt["shifts"] == []


def test_the_dashboard_says_what_holds_up_a_day_not_closed_at_the_change():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    options(a, dip_at_shift_end=True, close_day_at_change="18:00")
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": DAY_START}
    sid = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    cb.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json")
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": HANDOVER, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150")}, format="json")  # no dip
    with db_context(tenant_id=a["tenant"].id):
        station = Station.objects.get(pk=fc.station.pk)
        assert dayclose.unclosed_change(station, "18:00", now=_local(2026, 10, 1, 17)) is None  # 19:00: still time
        held = dayclose.unclosed_change(station, "18:00", now=_local(2026, 10, 1, 18, 30))  # 20:30
        assert held == {"day": "2026-10-01", "change": "18:00", "people": [att_b.full_name], "tanks": [fc.tank.name]}
        assert dayclose.unclosed_change(station, "18:00", now=_local(2026, 10, 1, 23)) == held  # after midnight: still that day
