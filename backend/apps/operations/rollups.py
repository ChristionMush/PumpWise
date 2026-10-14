"""
Rule 9: dashboards read rollup tables, never raw readings.

The rollups are written on the write path:
* a shift closes (or a closed shift is corrected): that station's daily sales for the affected days, the shift's
  summary (litres, cash check per payment type and per attendant), and the station's tank status;
* a dip or delivery is saved: that tank's status (estimated level, forecast, 7-day unexplained loss);
* an hourly job refreshes tank status, because forecasts age even when nothing is recorded.

Bulk loads (demo seeding) wrap their work in `deferred()`, which collects what changed and refreshes each item once.
Every refresh also bumps the tenant's dashboard cache version, so the next dashboard read is fresh.
"""
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Sum
from django.utils import timezone

from .models import DailySalesRollup, Shift, ShiftSummary, TankStatus, Transaction

_state = threading.local()


def dashboard_version(tenant_id):
    return cache.get(f"dashver:{tenant_id}") or "0"


def _bump(tenant_id):
    cache.set(f"dashver:{tenant_id}", uuid.uuid4().hex, None)


@contextmanager
def deferred():
    """Collect rollup work and do each piece once when the block ends (only if it ends without an error)."""
    if getattr(_state, "pending", None) is not None:
        yield
        return
    pending = _state.pending = {"days": set(), "tanks": set(), "shifts": set()}
    try:
        yield
    finally:
        _state.pending = None
    _flush(pending)


def _flush(pending):
    from apps.stations.models import Station, Tank

    stations = {s.pk: s for s in Station.all_objects.filter(pk__in={sid for sid, _ in pending["days"]})}
    for station_id, day in sorted(pending["days"], key=lambda x: (str(x[0]), x[1])):
        refresh_daily(stations[station_id], day)
    for shift in Shift.objects.filter(pk__in=pending["shifts"]).select_related("station"):
        refresh_shift(shift)
    for tank in Tank.objects.filter(pk__in=pending["tanks"]).select_related("fuel_type"):
        refresh_tank(tank)


def _days_of(shift):
    days = {timezone.localtime(t).date() for t in Transaction.objects.filter(shift=shift).values_list("occurred_at", flat=True)}
    days |= {timezone.localtime(t).date() for t in Transaction.all_objects.filter(shift=shift).values_list("occurred_at", flat=True)}
    if shift.ended_at:
        days.add(timezone.localtime(shift.ended_at).date())
    return days


def on_shift_closed(shift):
    from apps.stations.models import Tank

    days = _days_of(shift)
    tanks = list(Tank.objects.filter(station_id=shift.station_id).select_related("fuel_type"))
    pending = getattr(_state, "pending", None)
    if pending is not None:
        pending["days"] |= {(shift.station_id, d) for d in days}
        pending["shifts"].add(shift.pk)
        pending["tanks"] |= {t.pk for t in tanks}
        return
    for d in days:
        refresh_daily(shift.station, d)
    refresh_shift(shift)
    for t in tanks:
        refresh_tank(t)


def on_tank_changed(tank):
    pending = getattr(_state, "pending", None)
    if pending is not None:
        pending["tanks"].add(tank.pk)
        return
    refresh_tank(tank)


def refresh_daily(station, day):
    from apps.reports.sales import local_day_bounds

    start, end = local_day_bounds(day)
    rows = (
        Transaction.objects.filter(station=station, occurred_at__gte=start, occurred_at__lt=end)
        .values("fuel_type_id", "currency").annotate(litres=Sum("litres"), amount=Sum("amount")).order_by()
    )
    existing = {(r.fuel_type_id, r.currency): r for r in DailySalesRollup.objects.filter(station=station, day=day)}
    for r in rows:
        key = (r["fuel_type_id"], r["currency"])
        obj = existing.pop(key, None)
        if obj is None:
            DailySalesRollup.objects.create(tenant_id=station.tenant_id, station=station, day=day, fuel_type_id=key[0], currency=key[1], litres=r["litres"], amount=r["amount"])
        elif (obj.litres, obj.amount) != (r["litres"], r["amount"]):
            obj.litres, obj.amount = r["litres"], r["amount"]
            obj.save(update_fields=["litres", "amount", "updated_at"])
    for obj in existing.values():  # nothing sold any more for this fuel/currency that day (e.g. a corrected shift)
        obj.delete()
    _bump(station.tenant_id)


def refresh_shift(shift):
    from apps.reports.reconciliation import cash_summary

    summary = cash_summary(shift)
    litres = Transaction.objects.filter(shift=shift).aggregate(s=Sum("litres"))["s"] or Decimal(0)
    values = {"station": shift.station, "day": timezone.localtime(shift.started_at).date(), "litres": litres,
              "flagged": bool(summary["flagged"]), "cash": summary, "computed_at": timezone.now()}
    obj = ShiftSummary.objects.filter(shift=shift).first()
    if obj is None:
        ShiftSummary.objects.create(tenant_id=shift.tenant_id, shift=shift, **values)
    else:
        for k, v in values.items():
            setattr(obj, k, v)
        obj.save()
    _bump(shift.tenant_id)


def _json_safe(data):
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in data.items()}


def refresh_tank(tank, now=None, bump=True):
    from apps.insights.forecast import forecast_tank
    from apps.reports.reconciliation import tank_range

    now = now or timezone.now()
    today = timezone.localtime(now).date()
    data = forecast_tank(tank, now=now)
    losses = [r for r in tank_range([tank], today - timedelta(days=6), today) if r["status"] == "loss"]
    data["loss_7d"] = str(sum((Decimal(r["loss"]) for r in losses), Decimal(0)))
    data = _json_safe(data)
    obj = TankStatus.objects.filter(tank=tank).first()
    if obj is None:
        obj = TankStatus.objects.create(tenant_id=tank.tenant_id, tank=tank, data=data, computed_at=now)
    else:
        obj.data, obj.computed_at = data, now
        obj.save(update_fields=["data", "computed_at", "updated_at"])
    if bump:
        _bump(tank.tenant_id)
    return obj


def refresh_tenant_tanks(tenant_id=None):
    """Hourly: forecasts move with time even when nothing is recorded."""
    from apps.stations.models import Tank

    for tank in Tank.objects.select_related("fuel_type"):
        refresh_tank(tank)
