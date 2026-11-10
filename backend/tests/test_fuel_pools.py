"""Fuel pools: every pump of a fuel draws from every tank of that fuel at its station, so the tanks of one fuel are one
stock in the fuel check, the forecast and the stock figures. A fuel with one tank keeps its tank's name and figures."""
from datetime import date
from decimal import Decimal

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.insights.forecast import forecast_tank
from apps.operations import services
from apps.reports.reconciliation import reconcile_tank_day, tank_range
from apps.stations.models import Tank
from apps.stations.pools import pools_for

from .helpers import at, make_forecourt, run_shift, second_tank, two_tenants


def test_the_petrol_tanks_are_one_stock_for_the_fuel_check_and_the_forecast():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.50")  # Tank 1, petrol: the pump's nozzles are on it
    tank2 = second_tank(fc, code="PETROL", name="Tank 2")  # a second petrol tank: the pump draws from it too
    diesel = second_tank(fc, code="DIESEL", name="Diesel Tank")  # one tank of its fuel: as before
    mgr = a[Role.MANAGER]
    with db_context(tenant_id=a["tenant"].id):
        for tank, litres, when in ((fc.tank, "5000", at(2026, 10, 1, 3)), (tank2, "3000", at(2026, 10, 1, 3)), (diesel, "1000", at(2026, 10, 1, 3))):
            services.record_tank_dip(user=mgr, tank=tank, litres=Decimal(litres), at=when)
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=at(2026, 10, 1, 5), ended_at=at(2026, 10, 1, 15), cash=[("cash", "USD", 150)])
    with db_context(tenant_id=a["tenant"].id):
        # the 100 L came out of both petrol tanks (50 each): per tank one would look like a gain, the other a loss
        for tank, litres in ((fc.tank, "4950"), (tank2, "2950"), (diesel, "1000")):
            services.record_tank_dip(user=mgr, tank=tank, litres=Decimal(litres), at=at(2026, 10, 1, 18))
        tanks = list(Tank.objects.select_related("fuel_type", "station").order_by("name"))
        assert [p.name for p in pools_for(tanks)] == ["Diesel Tank", "Tank 1 + Tank 2"]
        petrol = reconcile_tank_day(fc.tank, date(2026, 10, 1))
        assert (petrol["tank"], petrol["opening"], petrol["dispensed"], petrol["closing"], petrol["loss"], petrol["status"]) == (
            "Tank 1 + Tank 2", "8000.000", "100.000", "7900.000", "0.000", "ok")
        assert reconcile_tank_day(tank2, date(2026, 10, 1)) == petrol  # either tank gives the pool
        assert reconcile_tank_day(diesel, date(2026, 10, 1))["tank"] == "Diesel Tank"
        assert len(tank_range(tanks, date(2026, 10, 1), date(2026, 10, 1))) == 2  # one row per fuel, not per tank
        f = forecast_tank(tank2, now=at(2026, 10, 1, 19))
        assert (f["tank"], f["current_litres"], f["capacity_litres"]) == ("Tank 1 + Tank 2", "7900.0", str(fc.tank.capacity_litres + tank2.capacity_litres))
