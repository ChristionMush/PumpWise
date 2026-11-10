"""A nozzle whose tank runs dry switches to another tank of the same fuel, at the meter number then: the litres before
the switch come out of the old tank, the litres after it out of the new one, in every per-tank figure."""
import uuid
from decimal import Decimal

from django.db.models import Sum

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.operations.models import TankSwitch, Transaction
from apps.stations.models import Nozzle

from .helpers import client_for, make_forecourt, make_user, second_tank, two_tenants
from .test_multi_attendant import cash, op, readings

T0, T_SWITCH, T_END = "2026-10-01T05:00:00Z", "2026-10-01T10:00:00Z", "2026-10-01T14:00:00Z"


def site():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")  # Pump 1: two nozzles on Tank 1 (petrol)
    backup = second_tank(fc, code="PETROL", name="Tank 2")  # the same fuel
    diesel = second_tank(fc, code="DIESEL", name="Diesel Tank")
    return a, fc, backup, diesel


def litres_by_tank(a):
    with db_context(tenant_id=a["tenant"].id):
        return {r["tank__name"]: r["s"] for r in Transaction.objects.values("tank__name").annotate(s=Sum("litres"))}


def test_the_attendant_on_the_pump_switches_to_a_backup_tank_and_the_litres_split_at_the_meter_number():
    a, fc, backup, diesel = site()
    att = client_for(a[Role.ATTENDANT])
    sid = att.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": T0, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    nozzle = fc.nozzles[0]
    options = next(n for p in att.get("/api/shifts/prefill/").json()["stations"][0]["pumps"] for n in p["nozzles"] if n["id"] == str(nozzle.pk))["switch_to"]
    assert options == [{"id": str(backup.pk), "name": "Tank 2"}]  # same fuel only, names only
    body = {"client_id": str(uuid.uuid4()), "nozzle": str(nozzle.pk), "tank": str(backup.pk), "reading": "60", "at": T_SWITCH}
    made = att.post("/api/tank-switches/", body, format="json")
    assert made.status_code == 201, made.content
    assert att.post("/api/tank-switches/", body, format="json").status_code == 200  # a resend records it once
    att.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150")}, format="json")
    assert litres_by_tank(a) == {"Tank 1": Decimal("60.000"), "Tank 2": Decimal("40.000")}  # 0-60 from Tank 1, 60-100 from Tank 2
    with db_context(tenant_id=a["tenant"].id):
        assert Nozzle.objects.get(pk=nozzle.pk).tank_id == backup.pk  # it draws from Tank 2 from now on
        assert TankSwitch.objects.count() == 1


def test_a_switch_is_refused_for_another_fuel_a_lower_number_or_someone_not_on_the_pump():
    a, fc, backup, diesel = site()
    att = client_for(a[Role.ATTENDANT])
    nozzle = str(fc.nozzles[0].pk)
    other = make_user(a["tenant"], Role.ATTENDANT, full_name="Not On Shift")
    assert client_for(other).post("/api/tank-switches/", {"nozzle": nozzle, "tank": str(backup.pk), "reading": "10"}, format="json").status_code == 403
    att.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": T0, "readings": readings(fc.nozzles, [100, 0])}, format="json")
    wrong_fuel = att.post("/api/tank-switches/", {"nozzle": nozzle, "tank": str(diesel.pk), "reading": "150", "at": T_SWITCH}, format="json")
    assert wrong_fuel.status_code == 400 and "same fuel" in str(wrong_fuel.json())
    lower = att.post("/api/tank-switches/", {"nozzle": nozzle, "tank": str(backup.pk), "reading": "50", "at": T_SWITCH}, format="json")
    assert lower.status_code == 400 and "below the last number" in str(lower.json())
    # a manager may switch it; offline, it goes through the sync queue like any phone entry
    synced = att.post("/api/sync/", {"ops": [op("tank_switch", {"client_id": str(uuid.uuid4()), "nozzle": nozzle, "tank": str(backup.pk), "reading": "150", "at": T_SWITCH})]},
                      format="json")
    assert synced.json()["results"][0]["status"] == "applied", synced.content
    back = client_for(a[Role.MANAGER]).post("/api/tank-switches/", {"nozzle": nozzle, "tank": str(fc.tank.pk), "reading": "160"}, format="json")
    assert back.status_code == 201, back.content


def test_the_close_to_close_fuel_check_splits_a_switched_nozzle_between_its_tanks():
    from apps.reports.daily import meter_fuel_checks
    from apps.tenants.services import get_settings

    a, fc, backup, diesel = site()
    mgr, att = client_for(a[Role.MANAGER]), client_for(a[Role.ATTENDANT])
    tanks = lambda one, two, three: [{"tank": str(fc.tank.pk), "dip_mm": "500", "litres": one}, {"tank": str(backup.pk), "dip_mm": "300", "litres": two},  # noqa: E731
                                     {"tank": str(diesel.pk), "dip_mm": "100", "litres": three}]
    meters = lambda one, two: [{"nozzle": str(fc.nozzles[0].pk), "reading": one}, {"nozzle": str(fc.nozzles[1].pk), "reading": two}]  # noqa: E731
    first = mgr.post("/api/day-close/", {"station": str(fc.station.pk), "at": "2026-10-01T04:00:00Z", "dips": tanks("5000", "3000", "1000"), "meters": meters("0", "0")}, format="json")
    assert first.status_code == 201, first.content
    sid = att.post("/api/shifts/start/", {"station": str(fc.station.pk), "started_at": T0, "readings": readings(fc.nozzles, [0, 0])}, format="json").json()["id"]
    att.post("/api/tank-switches/", {"nozzle": str(fc.nozzles[0].pk), "tank": str(backup.pk), "reading": "60", "at": T_SWITCH}, format="json")
    att.post(f"/api/shifts/{sid}/end/", {"ended_at": T_END, "readings": readings(fc.nozzles, [100, 0]), "cash": cash("150")}, format="json")
    second = mgr.post("/api/day-close/", {"station": str(fc.station.pk), "at": "2026-10-01T19:10:00Z", "dips": tanks("4940", "2960", "1000"), "meters": meters("100", "0")},
                      format="json")
    assert second.status_code == 201, second.content
    from apps.operations.models import DayClose

    with db_context(tenant_id=a["tenant"].id):
        opening, close = DayClose.objects.order_by("closed_at")
        checks = {c["tank"]: c for c in meter_fuel_checks(fc.station, opening, close, get_settings(a["tenant"].id))}
    assert (checks["Tank 1"]["dispensed"], checks["Tank 1"]["loss"]) == ("60.000", "0.000")
    assert (checks["Tank 2"]["dispensed"], checks["Tank 2"]["loss"]) == ("40.000", "0.000")
