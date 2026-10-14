"""Rule 9: dashboards read rollup tables written when a shift closes, never raw readings, with a short per-tenant cache."""
from datetime import timedelta
from decimal import Decimal

from django.db import connections, transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.operations import rollups
from apps.operations.models import DailySalesRollup, ShiftSummary, TankStatus

from .helpers import client_for, dip, make_forecourt, run_shift, two_tenants


def test_closing_a_shift_writes_the_rollups():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="2.00")
    now = timezone.now()
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 50], started_at=now - timedelta(minutes=9), ended_at=now - timedelta(minutes=2), cash=[("cash", "USD", 250)])
    with db_context(tenant_id=a["tenant"].id):
        day = DailySalesRollup.objects.get(station=fc.station, day=timezone.localdate())
        assert (day.litres, day.amount, day.currency) == (Decimal("150.000"), Decimal("300.00"), "USD")
        summary = ShiftSummary.objects.get()
        assert summary.litres == Decimal("150.000") and summary.flagged is True and summary.cash["variance"] == "-50.00"
        assert TankStatus.objects.filter(tank=fc.tank).exists()


def test_the_dashboard_reads_rollups_not_raw_transactions():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="2.00")
    now = timezone.now()
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=now - timedelta(minutes=9), ended_at=now - timedelta(minutes=2))
    # change a raw row behind the rollups' back (as the owner role, inside A's context): the dashboard must not see it
    with transaction.atomic(using="owner"), connections["owner"].cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, true)", [str(a["tenant"].id)])
        cur.execute("UPDATE operations_transaction SET litres = 999, amount = 1998")
    d = client_for(a[Role.OWNER]).get("/api/dashboard/").json()
    assert d["totals"]["today_litres"] == "100.000" and d["data_updated_at"] and d["as_of"]


def test_the_dashboard_is_cached_and_a_closed_shift_refreshes_it():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00")
    owner = client_for(a[Role.OWNER])
    first = owner.get("/api/dashboard/").json()
    assert owner.get("/api/dashboard/").json()["as_of"] == first["as_of"]  # served from the cache
    now = timezone.now()
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [40, 0], started_at=now - timedelta(minutes=9), ended_at=now - timedelta(minutes=2))
    fresh = owner.get("/api/dashboard/").json()
    assert fresh["totals"]["today_litres"] == "40.000" and fresh["as_of"] != first["as_of"]


def test_a_dip_refreshes_the_tank_status():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    dip(a["tenant"], a[Role.MANAGER], fc, 4321, timezone.now() - timedelta(minutes=1))
    with db_context(tenant_id=a["tenant"].id):
        assert TankStatus.objects.get(tank=fc.tank).data["current_litres"] == "4321.0"


def test_deferred_rollups_are_written_once_at_the_end():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00")
    now = timezone.now()
    with db_context(tenant_id=a["tenant"].id), rollups.deferred():
        run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [10, 0], started_at=now - timedelta(minutes=9), ended_at=now - timedelta(minutes=6))
        run_shift(a["tenant"], a[Role.ATTENDANT], fc, [10, 0], [30, 0], started_at=now - timedelta(minutes=5), ended_at=now - timedelta(minutes=2))
        assert not DailySalesRollup.objects.exists()  # nothing yet
    with db_context(tenant_id=a["tenant"].id):
        assert DailySalesRollup.objects.get(station=fc.station, day=timezone.localdate()).litres == Decimal("30.000")
        assert ShiftSummary.objects.count() == 2


def test_rollups_are_isolated_per_tenant():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00")
    now = timezone.now()
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [10, 0], started_at=now - timedelta(minutes=9), ended_at=now - timedelta(minutes=2))
    with db_context(tenant_id=b["tenant"].id):
        assert not DailySalesRollup.objects.exists() and not ShiftSummary.objects.exists() and not TankStatus.objects.exists()
    assert client_for(b[Role.OWNER]).get("/api/dashboard/").json()["totals"]["today_litres"] == "0"
