"""Phase 7: how long will each tank last?

days to empty = (current level - reserve) / average daily usage
The weighted version replays the coming days using the tank's own weekday pattern (Saturdays differ from Tuesdays).
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.reports.sales import tank_daily_litres
from apps.stations.pools import pool_of
from apps.tenants.services import get_settings

HISTORY_DAYS = 28


def estimated_level(tank, now=None):
    """Last dip, plus deliveries since, minus litres sold since. None if the tank was never dipped."""
    now = now or timezone.now()
    tank = pool_of(tank)  # every tank of the fuel at the station: one stock
    last = tank.dip_at(now)
    if last is None:
        return None, None
    delivered = tank.delivered(last.reading_at, now)
    sold = tank.sold(last.reading_at, now)
    return max(last.litres + delivered - sold, Decimal(0)), last.reading_at


def usage_profile(tank, today):
    """(average litres/day, {weekday: average}) over recent history, counting quiet days but not days before the tank was in use."""
    tank = pool_of(tank)
    history = tank_daily_litres(tank, days=HISTORY_DAYS, until=today - timedelta(days=1))
    first = tank.sales().order_by("occurred_at").values_list("occurred_at", flat=True).first()
    if first is None:
        return Decimal(0), {}
    start = timezone.localtime(first).date()
    history = {d: v for d, v in history.items() if d >= start}
    if not history:
        return Decimal(0), {}
    overall = sum(history.values(), Decimal(0)) / len(history)
    by_wd = defaultdict(list)
    for d, v in history.items():
        by_wd[d.weekday()].append(v)
    return overall, {wd: sum(v, Decimal(0)) / len(v) for wd, v in by_wd.items()}


def forecast_tank(tank, cfg=None, now=None):
    tank = pool_of(tank)  # per fuel pool: its name, capacity and reserve are its tanks' together
    cfg = cfg or get_settings(tank.tenant_id)
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    level, as_of = estimated_level(tank, now)
    out = {"tank_id": str(tank.pk), "tank": tank.name, "station_id": str(tank.station_id), "fuel_code": tank.fuel_type.code,
           "capacity_litres": str(tank.capacity_litres), "reserve_litres": str(tank.reserve_litres)}
    if level is None:
        return {**out, "status": "no_dip"}
    available = max(level - tank.reserve_litres, Decimal(0))
    overall, profile = usage_profile(tank, today)
    out.update({
        "status": "ok", "current_litres": str(level.quantize(Decimal("0.1"))), "as_of": as_of, "available_litres": str(available.quantize(Decimal("0.1"))),
        "level_pct": round(float(level / tank.capacity_litres * 100), 1), "avg_daily_litres": str(overall.quantize(Decimal("0.1"))),
        "days_to_empty_simple": None, "days_to_empty": None, "empty_date": None, "refill_by": None,
    })
    if overall > 0:
        simple = available / overall
        out["days_to_empty_simple"] = round(float(simple), 1)
        remaining, days, d = available, Decimal(0), today
        for _ in range(366):
            use = profile.get(d.weekday(), overall) or overall
            if use <= 0:
                use = overall
            if remaining <= use:
                days += remaining / use
                break
            remaining -= use
            days += 1
            d += timedelta(days=1)
        else:
            days = None
        if days is not None:
            empty = today + timedelta(days=int(days))
            out.update({"days_to_empty": round(float(days), 1), "empty_date": empty.isoformat(), "refill_by": (empty - timedelta(days=cfg.refill_lead_days)).isoformat()})
    out["low"] = bool(level <= tank.reserve_litres or (out["days_to_empty"] is not None and out["days_to_empty"] <= cfg.low_stock_days))
    return out
