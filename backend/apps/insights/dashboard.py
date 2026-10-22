"""The owner's phone dashboard (Phase 6).

Rule 9: it reads only the rollup tables written when a shift closes or a dip/delivery is saved (apps/operations/
rollups.py), never raw readings, and each tenant's dashboard is cached for 45 seconds. The cache key carries a version
that every rollup write bumps, so a newly closed shift shows up at once. `as_of` says when the numbers were built and
`data_updated_at` when the newest rollup behind them was written.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone

from apps.accounts.permissions import sees_all_stations
from apps.operations.models import DailySalesRollup, Shift, ShiftAttendant, ShiftSummary, TankStatus
from apps.operations.rollups import dashboard_version, refresh_tank
from apps.stations.access import scope, visible_station_ids
from apps.stations.models import Station, Tank

from .models import Alert

CACHE_SECONDS = 45
TANK_KEYS = ("tank_id", "tank", "fuel_code", "current_litres", "capacity_litres", "level_pct", "days_to_empty", "refill_by", "low", "status", "as_of")


def _cache_key(user):
    ids = visible_station_ids(user)
    scope_key = "all" if ids is None else ",".join(sorted(str(i) for i in ids))
    return f"dashboard:{user.tenant_id}:{dashboard_version(user.tenant_id)}:{scope_key}"


def build_dashboard(user):
    key = _cache_key(user)
    cached = cache.get(key)
    if cached is not None:
        return cached
    body = _build(user)
    cache.set(key, body, CACHE_SECONDS)
    return body


def _build(user):
    from apps.operations.tradingday import Calendar

    now = timezone.now()
    stations = list(scope(Station.objects.filter(status="active"), user, "id"))
    station_ids = [s.pk for s in stations]
    today_of = {s.pk: Calendar(s.pk).today() for s in stations}  # each station's trading day (closes about 21:00)
    today = max(today_of.values(), default=Calendar().today())
    tanks = list(Tank.objects.filter(station_id__in=station_ids).select_related("fuel_type"))
    statuses = {s.tank_id: s for s in TankStatus.objects.filter(tank__in=tanks)}
    for t in tanks:
        if t.pk not in statuses:  # a tank set up since the last write: build its row now (write path, once)
            statuses[t.pk] = refresh_tank(t, bump=False)  # no bump: this very read is about to be cached
    sales = [r for r in DailySalesRollup.objects.filter(station_id__in=station_ids, day__in=set(today_of.values())) if r.day == today_of[r.station_id]]
    flagged_cash = ShiftSummary.objects.filter(station_id__in=station_ids, day__gte=today - timedelta(days=6), flagged=True).count()
    open_shifts = list(Shift.objects.filter(station_id__in=station_ids, status="open").order_by("started_at"))
    people = {}
    for a in ShiftAttendant.objects.filter(shift__in=open_shifts).select_related("user").order_by("-is_lead", "joined_at"):
        people.setdefault(a.shift_id, []).append(a)

    updated = [s.computed_at for s in statuses.values()]
    for model in (DailySalesRollup, ShiftSummary):
        latest = model.objects.filter(station_id__in=station_ids).aggregate(m=Max("updated_at"))["m"]
        if latest:
            updated.append(latest)

    out_stations, total_litres, total_amount, total_loss = [], Decimal(0), {}, Decimal(0)
    for st in stations:
        rows = [r for r in sales if r.station_id == st.pk]
        litres = sum((r.litres for r in rows), Decimal(0))
        amounts = {}
        for r in rows:
            amounts[r.currency] = amounts.get(r.currency, Decimal(0)) + r.amount
        total_litres += litres
        for c, a in amounts.items():
            total_amount[c] = total_amount.get(c, Decimal(0)) + a
        st_tanks = [statuses[t.pk].data for t in tanks if t.station_id == st.pk]
        loss = sum((Decimal(d.get("loss_7d") or "0") for d in st_tanks), Decimal(0))
        total_loss += loss
        out_stations.append({
            "id": str(st.pk), "name": st.name,
            "today": {"litres": str(litres.quantize(Decimal("0.001"))), "amount_by_currency": {c: str(a.quantize(Decimal("0.01"))) for c, a in amounts.items()}},
            "tanks": [{k: d.get(k) for k in TANK_KEYS} for d in st_tanks],
            "open_shifts": [{
                "id": str(s.pk), "started_at": s.started_at, "hours_open": round((now - s.started_at).total_seconds() / 3600, 1),
                "attendants": [a.user.full_name for a in people.get(s.pk, []) if a.left_at is None],
                "attendant": next((a.user.full_name for a in people.get(s.pk, []) if a.is_lead), None),
            } for s in open_shifts if s.station_id == st.pk],
            "unexplained_loss_7d": str(loss),
        })
    refills = sorted((d for d in (s.data for s in statuses.values()) if d.get("refill_by")), key=lambda d: d["refill_by"])
    open_alerts = Alert.objects.filter(status="open")
    if not sees_all_stations(user):
        open_alerts = open_alerts.filter(station_id__in=station_ids)
    return {
        "as_of": now,
        "data_updated_at": max(updated) if updated else None,
        "totals": {
            "today_litres": str(total_litres.quantize(Decimal("0.001"))) if total_litres else "0",
            "today_amount_by_currency": {c: str(a.quantize(Decimal("0.01"))) for c, a in total_amount.items()},
            "open_alerts": open_alerts.count(), "unexplained_loss_7d": str(total_loss), "flagged_cash_shifts_7d": flagged_cash,
            "open_shifts": sum(len(s["open_shifts"]) for s in out_stations),
        },
        "next_refill": ({"tank": refills[0]["tank"], "refill_by": refills[0]["refill_by"], "days_to_empty": refills[0]["days_to_empty"]} if refills else None),
        "stations": out_stations,
        "alerts": [{"id": str(a.pk), "kind": a.kind, "severity": a.severity, "message": a.message, "created_at": a.created_at} for a in open_alerts.order_by("-created_at")[:5]],
    }
