"""Phase 5: do the numbers add up?

Tank:  opening + deliveries - dispensed - closing = unexplained loss (litres)
Cash:  what was handed in (all payment types, all currencies, converted at the owner's rate) vs litres x price
"""
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from apps.operations.models import Delivery, TankReading, Transaction
from apps.stations.models import Tank
from apps.stations.services import rate_at
from apps.tenants.services import get_settings

from .sales import local_day_bounds

Q2, Q3 = Decimal("0.01"), Decimal("0.001")


def cash_summary(shift):
    cfg = get_settings(shift.tenant_id)
    currency = shift.station.pricing_currency
    expected = shift.transactions.aggregate(s=Sum("amount"))["s"] or Decimal(0)
    at = shift.ended_at or shift.started_at
    lines, total, missing = [], Decimal(0), []
    for c in shift.cash.all():
        rate = rate_at(c.currency, currency, at)
        converted = (c.amount * rate).quantize(Q2) if rate is not None else None
        if converted is None:
            missing.append(c.currency)
        else:
            total += converted
        lines.append({"payment_type": c.payment_type, "currency": c.currency, "amount": str(c.amount), "converted": str(converted) if converted is not None else None})
    variance = total - expected
    pct = (variance / expected * 100).quantize(Q2) if expected else Decimal(0)
    limit = max(cfg.cash_variance_amount, expected * cfg.cash_variance_pct / 100)
    closed = shift.status != "open"
    flagged = closed and (abs(variance) > limit or bool(missing))
    status = "open" if not closed else "incomplete" if missing else "short" if variance < -limit else "over" if variance > limit else "ok"
    return {
        "currency": currency, "expected": str(expected.quantize(Q2)), "submitted_total": str(total.quantize(Q2)), "variance": str(variance.quantize(Q2)),
        "variance_pct": str(pct), "status": status, "flagged": flagged, "missing_rate": sorted(set(missing)), "by_payment_type": lines,
        "allowed_variance": str(limit.quantize(Q2)),
    }


def _latest_dip(tank, at):
    return TankReading.objects.filter(tank=tank, reading_at__lte=at).order_by("-reading_at").first()


def reconcile_window(tank, opening, closing, cfg=None):
    """Reconcile between two actual dips (so the window matches what was really measured)."""
    cfg = cfg or get_settings(tank.tenant_id)
    base = {"tank_id": str(tank.pk), "tank": tank.name, "fuel_code": tank.fuel_type.code}
    if opening is None or closing is None or closing.reading_at <= opening.reading_at:
        return {**base, "status": "insufficient_data", "flagged": False}
    deliveries = Delivery.objects.filter(tank=tank, delivered_at__gt=opening.reading_at, delivered_at__lte=closing.reading_at).aggregate(s=Sum("litres_received"))["s"] or Decimal(0)
    dispensed = Transaction.objects.filter(nozzle__pump__tank=tank, occurred_at__gt=opening.reading_at, occurred_at__lte=closing.reading_at).aggregate(s=Sum("litres"))["s"] or Decimal(0)
    loss = opening.litres + deliveries - dispensed - closing.litres
    limit = max(cfg.loss_threshold_litres, dispensed * cfg.loss_threshold_pct / 100)
    status = "loss" if loss > limit else "gain" if loss < -limit else "ok"
    return {
        **base, "window_start": opening.reading_at, "window_end": closing.reading_at, "opening": str(opening.litres), "deliveries": str(deliveries),
        "dispensed": str(dispensed.quantize(Q3)), "closing": str(closing.litres), "loss": str(loss.quantize(Q3)),
        "loss_pct": str((loss / dispensed * 100).quantize(Q2)) if dispensed else None, "allowed": str(limit.quantize(Q3)),
        "status": status, "flagged": status != "ok",
    }


def reconcile_tank_day(tank, day: date, cfg=None):
    start, end = local_day_bounds(day)
    opening = _latest_dip(tank, start)
    if opening is None:  # the tank's first-ever dip is today: open from the earliest dip of the day
        opening = TankReading.objects.filter(tank=tank, reading_at__gt=start, reading_at__lte=end).order_by("reading_at").first()
    return {"date": day.isoformat(), **reconcile_window(tank, opening, _latest_dip(tank, end), cfg)}


def tank_range(tanks, date_from: date, date_to: date):
    from datetime import timedelta

    out, n = [], date_from
    while n <= date_to:
        for t in tanks:
            row = reconcile_tank_day(t, n)
            if row["status"] != "insufficient_data":
                out.append(row)
        n += timedelta(days=1)
    return out


def tanks_for(user_scope_ids, station=None):
    qs = Tank.objects.select_related("fuel_type", "station")
    if user_scope_ids is not None:
        qs = qs.filter(station_id__in=user_scope_ids)
    if station:
        qs = qs.filter(station_id=station)
    return list(qs)
