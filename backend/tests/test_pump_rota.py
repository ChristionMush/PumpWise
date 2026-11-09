"""Weekly pump rotas: two crews, one week on and one week off, each with day and night people on their own pumps. The
manager assigns pumps by hand; the rota learns each assignment and repeats it before every later shift change."""
from datetime import date, datetime, timedelta

from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.operations import rota
from apps.operations.models import PumpRota, PumpRotaHolder
from apps.stations.models import Pump, Station

from .helpers import client_for, make_user, platform_client
from .test_multi_attendant import T0, T_SWAP, assigned_site, cash, readings, start

WEEK_STARTS = date(2026, 11, 9)  # a Monday: crew A this week, crew B next week


def local(d, h, mi=0):
    return timezone.make_aware(datetime(d.year, d.month, d.day, h, mi), timezone.get_current_timezone())


def test_shifts_and_crews_follow_the_clock():
    r = PumpRota(week_starts=WEEK_STARTS)
    mon = WEEK_STARTS
    assert rota.shift_at(local(mon, 7)) == (local(mon, 7), "day")
    assert rota.shift_at(local(mon, 17, 59)) == (local(mon, 7), "day")
    assert rota.shift_at(local(mon, 18)) == (local(mon, 18), "night")
    assert rota.shift_at(local(mon + timedelta(days=1), 6, 59)) == (local(mon, 18), "night")  # a night belongs to its evening
    assert rota.next_shift(local(mon, 10)) == (local(mon, 18), "night")
    assert rota.next_shift(local(mon, 23)) == (local(mon + timedelta(days=1), 7), "day")
    sunday_night, next_monday = mon + timedelta(days=6), mon + timedelta(days=7)
    assert rota.crew_of(r, local(sunday_night, 18)) == "a"  # the outgoing crew works the last night up to 07:00
    assert rota.crew_of(r, local(next_monday, 7)) == "b"
    assert rota.crew_of(r, local(next_monday + timedelta(days=7), 7)) == "a"
    assert rota.crew_of(r, local(mon - timedelta(days=1), 18)) == "b"  # and before the start day too


def pesnat(a, on=True):
    res = platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/features/", {"pesnat_mode": on}, format="json")
    assert res.status_code == 200 and res.json()["features"]["pesnat_mode"] is on


def rota_site():
    """Pump 1 and Pump 2; crew A has att_a (day, Pump 1), att_b (day, Pump 2) and a night person on both."""
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    pesnat(a)
    night = make_user(a["tenant"], Role.ATTENDANT, full_name="Night Attendant")
    on = mgr.put(f"/api/pump-rotas/{fc.station.pk}/", {"on": True, "week_starts": str(WEEK_STARTS)}, format="json")
    assert on.status_code == 200, on.content
    return a, fc, att_a, att_b, night, pump2, p2


def assign(a, pump, person, at, **extra):
    """A manager's assignment at a given moment (the API always uses now)."""
    from apps.operations.services import assign_pump

    with db_context(tenant_id=a["tenant"].id):
        return assign_pump(user=a[Role.MANAGER], pump=Pump.objects.get(pk=pump.pk), to_user=person, at=at, **extra)


def holders(a):
    with db_context(tenant_id=a["tenant"].id):
        return {(h.pump.name, h.crew, h.part): h.user.full_name for h in PumpRotaHolder.objects.select_related("pump", "user")}


def apply_at(a, at):
    with db_context(tenant_id=a["tenant"].id):
        return rota.apply_tenant_rotas(a["tenant"].id, at)


def holder_of(a, pump):
    with db_context(tenant_id=a["tenant"].id):
        return Pump.objects.select_related("assigned_to").get(pk=pump.pk).assigned_to


def test_manual_assignments_are_learned_for_their_crew_and_shift():
    a, fc, att_a, att_b, night, pump2, p2 = rota_site()
    mon = WEEK_STARTS
    assign(a, fc.pump, att_a, local(mon, 6, 30))  # before the day shift: for the day shift
    assign(a, pump2, att_b, local(mon, 9))  # during it: for it
    assign(a, fc.pump, night, local(mon, 17, 20))  # within the hour before the night: for the night
    assign(a, pump2, night, local(mon, 17, 20))
    week2 = mon + timedelta(days=7)
    assign(a, fc.pump, att_b, local(week2, 8))  # crew B's first day
    assert holders(a) == {
        ("Pump 1", "a", "day"): att_a.full_name, ("Pump 2", "a", "day"): att_b.full_name,
        ("Pump 1", "a", "night"): night.full_name, ("Pump 2", "a", "night"): night.full_name,
        ("Pump 1", "b", "day"): att_b.full_name,
    }


def test_a_handover_planned_while_the_pump_is_in_use_is_for_the_next_shift():
    a, fc, att_a, att_b, night, pump2, p2 = rota_site()
    started = start(client_for(att_a), fc, [100, 200])  # att_a is on Pump 1 now
    assert started.status_code == 201, started.content
    assign(a, fc.pump, night, local(WEEK_STARTS, 14))  # mid-afternoon, no meter numbers: att_a keeps it to the end
    assert holders(a)[("Pump 1", "a", "night")] == night.full_name
    assert ("Pump 1", "a", "day") not in holders(a)


def test_the_pumps_switch_an_hour_before_each_shift_change_once_learned():
    a, fc, att_a, att_b, night, pump2, p2 = rota_site()
    mon = WEEK_STARTS
    assign(a, fc.pump, att_a, local(mon, 6, 30))
    assign(a, pump2, att_b, local(mon, 6, 30))
    assign(a, fc.pump, night, local(mon, 17, 30))
    assign(a, pump2, night, local(mon, 17, 30))
    tue = mon + timedelta(days=1)
    assert apply_at(a, local(tue, 6)) == {str(fc.station.pk): ["Pump 1", "Pump 2"]}
    assert holder_of(a, fc.pump) == att_a and holder_of(a, pump2) == att_b
    assert apply_at(a, local(tue, 6, 30)) == {str(fc.station.pk): []}  # once per shift change
    assert apply_at(a, local(tue, 17)) == {str(fc.station.pk): ["Pump 1", "Pump 2"]}
    assert holder_of(a, fc.pump) == night and holder_of(a, pump2) == night
    with db_context(tenant_id=a["tenant"].id):
        auto = AuditLog.objects.filter(action="pump_assigned", actor__isnull=True)
        assert auto.count() == 4 and "crew A" in auto.first().changes["rota"]


def test_a_manual_change_stands_until_the_next_shift_change_and_is_learned():
    a, fc, att_a, att_b, night, pump2, p2 = rota_site()
    mon, tue = WEEK_STARTS, WEEK_STARTS + timedelta(days=1)
    assign(a, fc.pump, att_a, local(mon, 6, 30))
    assign(a, fc.pump, night, local(mon, 17, 30))
    apply_at(a, local(tue, 6))
    assign(a, fc.pump, att_b, local(tue, 9))  # att_a off sick: att_b covers Pump 1
    assert apply_at(a, local(tue, 10)) == {str(fc.station.pk): []}
    assert holder_of(a, fc.pump) == att_b
    apply_at(a, local(tue, 17))
    assert holder_of(a, fc.pump) == night
    apply_at(a, local(tue + timedelta(days=1), 6))
    assert holder_of(a, fc.pump) == att_b  # the cover was learned, as the manager chose


def test_crew_b_takes_over_next_week_and_unlearned_shifts_are_left_alone():
    a, fc, att_a, att_b, night, pump2, p2 = rota_site()
    mon, week2 = WEEK_STARTS, WEEK_STARTS + timedelta(days=7)
    crew_b = make_user(a["tenant"], Role.ATTENDANT, full_name="Crew B Day")
    assign(a, fc.pump, att_a, local(mon, 6, 30))
    assert apply_at(a, local(week2, 6)) == {str(fc.station.pk): []}  # crew B's day not learned yet
    assert holder_of(a, fc.pump) == att_a
    assign(a, fc.pump, crew_b, local(week2, 6, 30))
    apply_at(a, local(week2 + timedelta(days=7), 6))
    assert holder_of(a, fc.pump) == att_a  # crew A again
    apply_at(a, local(week2 + timedelta(days=14), 6))
    assert holder_of(a, fc.pump) == crew_b


def test_someone_who_left_is_not_given_pumps():
    a, fc, att_a, att_b, night, pump2, p2 = rota_site()
    mon = WEEK_STARTS
    assign(a, fc.pump, att_a, local(mon, 6, 30))
    assign(a, fc.pump, night, local(mon, 17, 30))
    with db_context(tenant_id=a["tenant"].id):
        att_a.deactivate()
    assert apply_at(a, local(mon + timedelta(days=1), 6)) == {str(fc.station.pk): []}
    assert holder_of(a, fc.pump) == night


def test_without_a_rota_nothing_is_learned_or_switched():
    a, fc, att_a, att_b, pump2, p2, mgr = assigned_site()
    assign(a, fc.pump, att_b, local(WEEK_STARTS, 9))
    assert holders(a) == {}
    assert apply_at(a, local(WEEK_STARTS, 17)) == {}


def test_the_rota_api_is_for_managers_and_shows_what_was_learned():
    a, fc, att_a, att_b, night, pump2, p2 = rota_site()
    assign(a, fc.pump, att_a, local(WEEK_STARTS, 6, 30))
    assert client_for(att_a).get("/api/pump-rotas/").status_code == 403
    assert client_for(att_a).put(f"/api/pump-rotas/{fc.station.pk}/", {"on": False}, format="json").status_code == 403
    body = client_for(a[Role.OWNER]).get("/api/pump-rotas/").json()
    assert len(body) == 1 and body[0]["on"] is True and body[0]["week_starts"] == str(WEEK_STARTS)
    pump1 = next(p for p in body[0]["pumps"] if p["name"] == "Pump 1")
    assert pump1["holders"]["a_day"]["name"] == att_a.full_name and pump1["holders"]["b_night"] is None
    off = client_for(a[Role.MANAGER]).put(f"/api/pump-rotas/{fc.station.pk}/", {"on": False}, format="json")
    assert off.status_code == 200 and off.json()["on"] is False
    assert apply_at(a, local(WEEK_STARTS + timedelta(days=1), 6)) == {}
    with db_context(tenant_id=a["tenant"].id):
        assert PumpRota.objects.count() == 1 and AuditLog.objects.filter(action="pump_rota_set").count() == 2


def test_the_rota_is_refused_on_shared_pumps():
    from .test_multi_attendant import setup

    a, fc, att_a, att_b = setup("multiple")
    pesnat(a)
    res = client_for(a[Role.MANAGER]).put(f"/api/pump-rotas/{fc.station.pk}/", {"on": True}, format="json")
    assert res.status_code == 400 and "shared" in str(res.json())


def test_a_pump_the_rota_allocated_is_not_listed_to_allocate():
    a, fc, att_a, att_b, night, pump2, p2 = rota_site()
    from apps.operations.services import pump_gaps

    ca = client_for(att_a)
    sid = start(ca, fc, [0, 0], started_at=T0).json()["id"]
    ca.post(f"/api/shifts/{sid}/end/", {"ended_at": T_SWAP, "readings": readings(fc.nozzles, [50, 0]), "cash": cash("75")}, format="json")
    with db_context(tenant_id=a["tenant"].id):
        station = Station.objects.get(pk=fc.station.pk)  # now on assigned pumps (fc.station is the object from before)
        assert [g["pump_name"] for g in pump_gaps([station]) if g["kind"] == "allocate"] == ["Pump 1"]
        # the hourly run gave Pump 1 to the night person for the shift coming up
        weekly = PumpRota.objects.get()
        nxt, part = rota.shift_at(timezone.now() + rota.LEAD)
        PumpRotaHolder.objects.create(tenant_id=a["tenant"].id, rota=weekly, pump=fc.pump, crew=rota.crew_of(weekly, nxt), part=part, user=night)
        Pump.objects.filter(pk=fc.pump.pk).update(assigned_to=night)
        assert [g for g in pump_gaps([station]) if g["kind"] == "allocate"] == []


def test_the_rota_is_part_of_pesnat_mode_and_stops_without_it():
    a, fc, att_a, att_b, night, pump2, p2 = rota_site()
    mon = WEEK_STARTS
    assign(a, fc.pump, att_a, local(mon, 6, 30))
    assign(a, fc.pump, night, local(mon, 17, 30))
    pesnat(a, on=False)
    mgr = client_for(a[Role.MANAGER])
    assert mgr.get("/api/pump-rotas/").json() == []
    refused = mgr.put(f"/api/pump-rotas/{fc.station.pk}/", {"on": True}, format="json")
    assert refused.status_code == 400 and "Pesnat mode" in str(refused.json())
    assert apply_at(a, local(mon + timedelta(days=1), 6)) == {}  # nothing switches
    assign(a, fc.pump, att_b, local(mon + timedelta(days=1), 9))  # nor is anything learned
    assert holders(a)[("Pump 1", "a", "day")] == att_a.full_name
    pesnat(a)  # back on: what it learned is still there
    assert apply_at(a, local(mon + timedelta(days=1), 17)) == {str(fc.station.pk): ["Pump 1"]}
