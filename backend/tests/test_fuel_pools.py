"""A nozzle can also draw from other tanks of its fuel ("Also draws from"). Tanks joined that way are one stock in the
fuel check, the forecast and the stock figures; a tank no nozzle shares keeps its own name and figures."""
from datetime import date
from decimal import Decimal

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.insights.forecast import forecast_tank
from apps.operations import services
from apps.reports.reconciliation import reconcile_tank_day, tank_range
from apps.stations.models import Tank
from apps.stations.pools import pools_for

from .helpers import at, client_for, make_forecourt, run_shift, second_tank, two_tenants


def dips(a, mgr, values, when):
    with db_context(tenant_id=a["tenant"].id):
        for tank, litres in values:
            services.record_tank_dip(user=mgr, tank=tank, litres=Decimal(litres), at=when)


def test_a_nozzle_that_also_draws_from_a_second_tank_makes_them_one_stock():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")  # Pump 1: two nozzles on Tank 1 (petrol)
    tank2 = second_tank(fc, code="PETROL", name="Tank 2")
    diesel = second_tank(fc, code="DIESEL", name="Diesel Tank")
    owner = client_for(a[Role.OWNER])
    for n in fc.nozzles:  # Setup: each petrol nozzle also draws from Tank 2
        res = owner.patch(f"/api/nozzles/{n.pk}/", {"also_draws_from": [str(tank2.pk)]}, format="json")
        assert res.status_code == 200, res.content
        assert res.json()["draws_from"] == ["Tank 1", "Tank 2"] and res.json()["also_draws_from"] == [str(tank2.pk)]
    wrong = owner.patch(f"/api/nozzles/{fc.nozzles[0].pk}/", {"also_draws_from": [str(diesel.pk)]}, format="json")
    assert wrong.status_code == 400 and "own fuel" in str(wrong.json())

    mgr = a[Role.MANAGER]
    dips(a, mgr, ((fc.tank, "5000"), (tank2, "3000"), (diesel, "1000")), at(2026, 10, 1, 3))
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=at(2026, 10, 1, 5), ended_at=at(2026, 10, 1, 15), cash=[("cash", "USD", 150)])
    dips(a, mgr, ((fc.tank, "4950"), (tank2, "2950"), (diesel, "1000")), at(2026, 10, 1, 18))  # the 100 L came out of both
    with db_context(tenant_id=a["tenant"].id):
        tanks = list(Tank.objects.select_related("fuel_type", "station").order_by("name"))
        assert [p.name for p in pools_for(tanks)] == ["Diesel Tank", "Tank 1 + Tank 2"]
        petrol = reconcile_tank_day(fc.tank, date(2026, 10, 1))
        assert (petrol["tank"], petrol["opening"], petrol["dispensed"], petrol["closing"], petrol["loss"], petrol["status"]) == (
            "Tank 1 + Tank 2", "8000.000", "100.000", "7900.000", "0.000", "ok")
        assert reconcile_tank_day(tank2, date(2026, 10, 1)) == petrol  # either tank gives the pool
        assert len(tank_range(tanks, date(2026, 10, 1), date(2026, 10, 1))) == 2  # one row for the joined tanks
        f = forecast_tank(tank2, now=at(2026, 10, 1, 19))
        assert (f["tank"], f["current_litres"], f["capacity_litres"]) == ("Tank 1 + Tank 2", "7900.0", "20000.000")

    # taken away again: each tank is its own stock
    for n in fc.nozzles:
        assert owner.patch(f"/api/nozzles/{n.pk}/", {"also_draws_from": []}, format="json").json()["draws_from"] == ["Tank 1"]
    with db_context(tenant_id=a["tenant"].id):
        assert [p.name for p in pools_for(tanks)] == ["Diesel Tank", "Tank 1", "Tank 2"]


def test_a_tank_no_nozzle_shares_keeps_its_own_figures():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")
    tank2 = second_tank(fc, code="PETROL", name="Tank 2")  # a second petrol tank, but no nozzle draws from it
    mgr = a[Role.MANAGER]
    dips(a, mgr, ((fc.tank, "5000"), (tank2, "3000")), at(2026, 10, 1, 3))
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=at(2026, 10, 1, 5), ended_at=at(2026, 10, 1, 15), cash=[("cash", "USD", 150)])
    dips(a, mgr, ((fc.tank, "4900"), (tank2, "3000")), at(2026, 10, 1, 18))
    with db_context(tenant_id=a["tenant"].id):
        one = reconcile_tank_day(fc.tank, date(2026, 10, 1))
        assert (one["tank"], one["dispensed"], one["loss"]) == ("Tank 1", "100.000", "0.000")
        two = reconcile_tank_day(tank2, date(2026, 10, 1))
        assert (two["tank"], two["dispensed"], two["loss"]) == ("Tank 2", "0.000", "0.000")
