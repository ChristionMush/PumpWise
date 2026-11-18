"""Module "low_stock_every_tank" (on in Mark 2, PESNAT): no low-stock alert while another tank of the same fuel still has
fuel; once every tank of the fuel is at or below a quarter of its capacity, one critical alert names each tank. Off (the
default, Mark 1, Fuel Zone): the fuel's days of stock left, as before (test_insights)."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.insights import alerts as alert_rules
from apps.insights.models import Alert
from apps.operations import services

from .helpers import make_forecourt, platform_client, second_tank, two_tenants


def module_on(a):
    res = platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/modules/", {"modules": {"low_stock_every_tank": True}}, format="json")
    assert res.status_code == 200, res.content


def dips(a, values, ago_minutes=1):
    when = timezone.now() - timedelta(minutes=ago_minutes)
    with db_context(tenant_id=a["tenant"].id):
        for tank, litres in values:
            services.record_tank_dip(user=a[Role.ATTENDANT], tank=tank, litres=Decimal(litres), at=when)


def low_alerts(a):
    with db_context(tenant_id=a["tenant"].id):
        alert_rules.evaluate_tenant(a["tenant"].id)
        return list(Alert.objects.filter(status="open", kind="low_stock").values_list("severity", "message"))


def test_one_low_tank_is_quiet_while_the_other_has_fuel_then_both_low_is_critical_and_a_delivery_clears_it():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])  # Tank 1, petrol, 10,000 L
    tank2 = second_tank(fc, code="PETROL", name="Tank 2")  # 10,000 L
    module_on(a)

    dips(a, ((fc.tank, "2000"), (tank2, "6000")), ago_minutes=30)  # Tank 1 at 20%, Tank 2 at 60%
    assert low_alerts(a) == []

    dips(a, ((tank2, "2500"),))  # 25%: low too
    [(severity, message)] = low_alerts(a)
    assert severity == "critical"
    assert message == "Main Station Petrol: every tank is low (Tank 1 2,000 L (20%), Tank 2 2,500 L (25%)). Order fuel now."

    with db_context(tenant_id=a["tenant"].id):
        services.record_delivery(user=a[Role.MANAGER], tank=fc.tank, litres=Decimal(5000), delivered_at=timezone.now())
    assert low_alerts(a) == []  # Tank 1 has fuel again: the alert clears by itself


def test_each_fuel_is_judged_on_its_own_tanks():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    tank2 = second_tank(fc, code="PETROL", name="Tank 2")
    diesel = second_tank(fc, code="DIESEL", name="Diesel Tank")
    module_on(a)
    dips(a, ((fc.tank, "6000"), (tank2, "1000"), (diesel, "800")))  # petrol: one tank still full; diesel: its only tank low
    [(severity, message)] = low_alerts(a)
    assert severity == "critical" and message.startswith("Main Station Diesel: every tank is low (Diesel Tank 800 L (8%))")


def test_a_tank_never_dipped_is_not_counted_as_low():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    second_tank(fc, code="PETROL", name="Tank 2")  # no dip yet
    module_on(a)
    dips(a, ((fc.tank, "1000"),))
    assert low_alerts(a) == []


def test_off_by_default_a_low_tank_alerts_by_its_reserve_as_before():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])  # reserve 500 L
    second_tank(fc, code="PETROL", name="Tank 2")
    dips(a, ((fc.tank, "400"),))
    assert low_alerts(a) == [("critical", "Main Station Tank 1: at or below the reserve (400.0 L left).")]
