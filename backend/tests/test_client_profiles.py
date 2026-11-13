"""How each live business works today, locked in before the switches become modules (docs/modules.md).

Each profile is that business's settings exactly as read from the live server on 2026-11-13. The tests set up a site
that way and check everything those settings decide: who gives out the pumps, whether expenses and the end-of-shift dip
are there, whether attendants see litres, and when the day closes. Moving the code onto modules must leave every one of
these passing, unchanged.

* Fuel Zone (Mark 1): the manager gives out the pumps and closes the day on the Dip screen from 21:00.
* PESNAT (Mark 2): attendants pick their pumps, dip at the end of their shift (with the litres shown) and record
  expenses; the day closes at the evening handover.
* Litrix (demo): PESNAT's way without the litres.
"""
from datetime import date

import pytest
from django.utils import timezone

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.operations.models import DayClose, TankReading
from apps.operations.tradingday import Calendar
from apps.stations.models import Nozzle, Pump

from .helpers import client_for, make_forecourt, make_user, platform_client, two_tenants
from .test_multi_attendant import cash, readings

# Settings exactly as on the live server (2026-11-13).
FUEL_ZONE = {"pump_system": "assigned", "multi_attendant_enabled": True, "pump_assignment_enabled": True, "attendants_pick_pumps": False,
             "expenses_enabled": False, "dip_at_shift_end": False, "attendants_see_dip_litres": False, "close_day_at_change": ""}
PESNAT = {**FUEL_ZONE, "attendants_pick_pumps": True, "expenses_enabled": True, "dip_at_shift_end": True, "attendants_see_dip_litres": True,
          "close_day_at_change": "18:00"}
LITRIX = {**PESNAT, "attendants_see_dip_litres": False}
PROFILES = {"fuel_zone": FUEL_ZONE, "pesnat": PESNAT, "litrix": LITRIX}

# 1 Oct 2026 in Harare (UTC+2): the day shift starts at 07:10; A hands over at 17:55 and B at 18:05.
DAY_START, A_ENDS, B_ENDS = "2026-10-01T05:10:00Z", "2026-10-01T15:55:00Z", "2026-10-01T16:05:00Z"
DAY = date(2026, 10, 1)


def site(profile):
    """A station set up like the business: two pumps on one tank, two attendants, the manager has given Pump 1 to A and
    Pump 2 to B (as Fuel Zone's manager does; at a business where attendants pick, the pick replaces it)."""
    a, _ = two_tenants()
    tid = a["tenant"].id
    fc = make_forecourt(a["tenant"])
    p = platform_client()
    assert p.patch(f"/api/platform/tenants/{tid}/pump-system/", {"pump_system": profile["pump_system"]}, format="json").status_code == 200
    options = {k: v for k, v in profile.items() if k not in ("pump_system", "multi_attendant_enabled", "pump_assignment_enabled")}
    res = p.patch(f"/api/platform/tenants/{tid}/features/", options, format="json")
    assert res.status_code == 200, res.content
    att_b = make_user(a["tenant"], Role.ATTENDANT, full_name="Second Attendant")
    with db_context(tenant_id=tid):
        pump2 = Pump.objects.create(tenant_id=tid, station=fc.station, name="Pump 2")
        p2 = [Nozzle.objects.create(tenant_id=tid, pump=pump2, tank=fc.tank, number=n) for n in (1, 2)]
    mgr = client_for(a[Role.MANAGER])
    assert mgr.post(f"/api/pumps/{fc.pump.pk}/assign/", {"user": str(a[Role.ATTENDANT].pk)}, format="json").status_code == 200
    assert mgr.post(f"/api/pumps/{pump2.pk}/assign/", {"user": str(att_b.pk)}, format="json").status_code == 200
    return a, fc, a[Role.ATTENDANT], att_b, pump2, p2


def prefill(user):
    return client_for(user).get("/api/shifts/prefill/").json()["stations"][0]


@pytest.mark.parametrize("name", PROFILES)
def test_the_platform_shows_the_business_set_up_exactly_as_live(name):
    profile = PROFILES[name]
    a, fc, *_ = site(profile)
    body = platform_client().get(f"/api/platform/tenants/{a['tenant'].id}/").json()
    assert body["pump_system"] == profile["pump_system"]
    assert body["features"] == {k: v for k, v in profile.items() if k != "pump_system"}
    assert {(s["attendants_mode"], s["assignment_mode"]) for s in body["stations"]} == {("multiple", "assigned")}
    assert client_for(a[Role.ATTENDANT]).get("/api/auth/me/").json()["tenant"]["expenses_enabled"] is profile["expenses_enabled"]


def test_fuel_zone_the_manager_gives_out_the_pumps_and_attendants_work_only_their_own():
    a, fc, att_a, att_b, pump2, p2 = site(FUEL_ZONE)
    mine = prefill(att_a)
    assert "pick_pumps" not in mine and [p["name"] for p in mine["pumps"]] == ["Pump 1"]
    ca = client_for(att_a)
    body = {"station": str(fc.station.pk), "started_at": DAY_START}
    assert ca.post("/api/shifts/start/", {**body, "readings": readings(p2, [0, 0])}, format="json").status_code == 400  # B's pump
    started = ca.post("/api/shifts/start/", {**body, "readings": readings(fc.nozzles, [0, 0])}, format="json")
    assert started.status_code == 201, started.content
    with db_context(tenant_id=a["tenant"].id):
        assert Pump.objects.get(pk=pump2.pk).assigned_to_id == att_b.pk  # still the manager's allocation


@pytest.mark.parametrize("name", ["pesnat", "litrix"])
def test_mark_2_attendants_pick_any_free_pump_when_they_start(name):
    a, fc, att_a, att_b, pump2, p2 = site(PROFILES[name])
    mine = prefill(att_a)
    assert mine["pick_pumps"] is True and {p["name"] for p in mine["pumps"]} == {"Pump 1", "Pump 2"}
    started = client_for(att_a).post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "pumps": [str(pump2.pk)],
                                                            "readings": readings(p2, [0, 0])}, format="json")
    assert started.status_code == 201, started.content
    with db_context(tenant_id=a["tenant"].id):
        assert Pump.objects.get(pk=pump2.pk).assigned_to_id == att_a.pk  # picked: theirs now


@pytest.mark.parametrize("name", PROFILES)
def test_expenses_only_where_switched_on(name):
    profile = PROFILES[name]
    a, fc, *_ = site(profile)
    res = client_for(a[Role.ATTENDANT]).post("/api/expenses/", {"station": str(fc.station.pk), "category": "lunch", "amount": "5"}, format="json")
    if profile["expenses_enabled"]:
        assert res.status_code == 201, res.content
    else:
        assert res.status_code == 400 and "not switched on" in str(res.json())


@pytest.mark.parametrize("name", PROFILES)
def test_what_an_attendants_phone_is_told_about_the_tanks(name):
    profile = PROFILES[name]
    a, fc, att_a, *_ = site(profile)
    mine = prefill(att_a)
    if not profile["dip_at_shift_end"]:  # Fuel Zone: nothing about tanks reaches an attendant (rule 11)
        assert "dip_at_end" not in mine and "tanks" not in mine
        return
    assert mine["dip_at_end"] is True and [t["name"] for t in mine["tanks"]] == [fc.tank.name]
    assert not any(k in t for t in mine["tanks"] for k in ("last_litres", "capacity_litres", "level_pct"))  # never the tank's level
    if profile["attendants_see_dip_litres"]:  # PESNAT: the dip chart, to show the litres as the mm is typed
        assert all(len(t["calibration"]) >= 2 for t in mine["tanks"])
    else:  # Litrix: the tanks to dip, without their charts
        assert not any("calibration" in t for t in mine["tanks"])


def handover(a, fc, att_a, att_b, pump2, p2, profile):
    """The day shift: A on Pump 1, B on Pump 2, both hand over at the 18:00 change. A dips where the business asks for it."""
    ca, cb = client_for(att_a), client_for(att_b)
    body = {"station": str(fc.station.pk), "started_at": DAY_START}
    pick = {"pumps": [str(fc.pump.pk)]} if profile["attendants_pick_pumps"] else {}
    sid = ca.post("/api/shifts/start/", {**body, **pick, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    pick = {"pumps": [str(pump2.pk)]} if profile["attendants_pick_pumps"] else {}
    assert cb.post("/api/shifts/start/", {**body, **pick, "readings": readings(p2, [0, 0])}, format="json").status_code == 201
    dips = {"dips": [{"tank": str(fc.tank.pk), "dip_mm": "500"}]} if profile["dip_at_shift_end"] else {}
    ended = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": A_ENDS, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150"), **dips}, format="json")
    assert ended.status_code == 200, ended.content
    ended = cb.post(f"/api/shifts/{sid}/end/", {"ended_at": B_ENDS, "readings": readings(p2, [40, 0]), "cash": cash("60")}, format="json")
    assert ended.status_code == 200, ended.content
    return sid


def test_fuel_zone_an_attendant_cannot_send_dips_with_their_end_of_shift():
    a, fc, att_a, *_ = site(FUEL_ZONE)
    ca = client_for(att_a)
    sid = ca.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": DAY_START, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    refused = ca.post(f"/api/shifts/{sid}/end/", {"ended_at": A_ENDS, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150"),
                                                  "dips": [{"tank": str(fc.tank.pk), "dip_mm": "500"}]}, format="json")
    assert refused.status_code == 403
    with db_context(tenant_id=a["tenant"].id):
        assert not TankReading.objects.exists()


def test_fuel_zone_the_18_00_handover_closes_nothing_the_manager_closes_the_day_from_21_00():
    a, fc, att_a, att_b, pump2, p2 = site(FUEL_ZONE)
    handover(a, fc, att_a, att_b, pump2, p2, FUEL_ZONE)
    with db_context(tenant_id=a["tenant"].id):
        assert not TankReading.objects.exists() and not DayClose.objects.exists()  # the 18:00 handover closes nothing
        cal = Calendar(fc.station.pk)
        assert cal.closes_at_change == ""
        assert timezone.localtime(cal.close_of(DAY)).strftime("%H:%M") == "21:00"  # until the manager closes it


@pytest.mark.parametrize("name", ["pesnat", "litrix"])
def test_mark_2_the_outgoing_attendant_dips_and_the_day_closes_at_the_evening_handover(name):
    profile = PROFILES[name]
    a, fc, att_a, att_b, pump2, p2 = site(profile)
    handover(a, fc, att_a, att_b, pump2, p2, profile)
    with db_context(tenant_id=a["tenant"].id):
        dip = TankReading.objects.get(recorded_by=att_a)
        assert str(dip.litres) == "5000.000"  # from the tank's chart, whatever the phone showed
        close = DayClose.objects.get()
        assert close.day == DAY and close.closed_by_id == att_b.pk and "18:00 shift change" in close.note
        cal = Calendar(fc.station.pk)
        assert cal.closes_at_change == "18:00" and cal.close_of(DAY) == close.closed_at
