"""Phase 5: do the numbers add up?

Tank:  opening + deliveries - dispensed - closing = unexplained loss (litres). Dispensed comes from the attendants' pump
       meters; dips and deliveries come from managers. Different people, so a mismatch is a stronger signal.
Cash:  what was handed in (all payment types, all currencies, converted at the owner's rate) vs litres x price.
       With several attendants, each person's hand-in is shown beside the total; on assigned-pump stations each person
       also has their own expected cash (litres from their pumps in their own periods x the price in force).
Wording is neutral on purpose: a "cash difference", never an accusation.
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


def _iso(dt):
    return dt.isoformat() if dt else None


def _status(closed, missing, difference, limit):
    if not closed:
        return "open"
    if missing:
        return "incomplete"
    return "short" if difference < -limit else "over" if difference > limit else "ok"


def cash_summary(shift):
    cfg = get_settings(shift.tenant_id)
    currency = shift.station.pricing_currency
    expected = shift.transactions.aggregate(s=Sum("amount"))["s"] or Decimal(0)
    at = shift.ended_at or shift.started_at
    lines, total, missing, by_person = [], Decimal(0), [], {}
    for c in shift.cash.select_related("attendant"):
        rate = rate_at(c.currency, currency, at)
        converted = (c.amount * rate).quantize(Q2) if rate is not None else None
        if converted is None:
            missing.append(c.currency)
        else:
            total += converted
            by_person[c.attendant_id] = by_person.get(c.attendant_id, Decimal(0)) + converted
        lines.append({"payment_type": c.payment_type, "currency": c.currency, "amount": str(c.amount), "converted": str(converted) if converted is not None else None,
                      "attendant": str(c.attendant_id) if c.attendant_id else None, "attendant_name": c.attendant.full_name if c.attendant_id else None})
    variance = total - expected
    pct = (variance / expected * 100).quantize(Q2) if expected else Decimal(0)

    def allowed(amount):
        return max(cfg.cash_variance_amount, amount * cfg.cash_variance_pct / 100)

    limit = allowed(expected)
    closed = shift.status != "open"

    # per attendant: always what each person handed in; expected per person only when pumps are assigned
    assigned = shift.assignments.exists()
    expected_by = {}
    if assigned:
        for row in shift.transactions.values("attendant_id").annotate(s=Sum("amount")).order_by():
            expected_by[row["attendant_id"]] = row["s"] or Decimal(0)
    periods = {}
    for a in shift.assignments.select_related("pump"):
        periods.setdefault(a.user_id, []).append({"pump": a.pump.name, "from": _iso(a.started_at), "to": _iso(a.ended_at)})
    people, person_flagged = [], False
    for a in shift.attendants.select_related("user"):
        submitted = by_person.get(a.user_id, Decimal(0))
        row = {"user": str(a.user_id), "name": a.user.full_name, "is_lead": a.is_lead, "joined_at": _iso(a.joined_at), "left_at": _iso(a.left_at),
               "handed_in": a.cash_submitted_at is not None, "submitted": str(submitted.quantize(Q2))}
        if assigned:
            mine = expected_by.get(a.user_id, Decimal(0))
            difference = submitted - mine
            status = _status(closed, missing, difference, allowed(mine))
            row.update({"expected": str(mine.quantize(Q2)), "cash_difference": str(difference.quantize(Q2)), "status": status,
                        "flagged": status not in ("ok", "open"), "periods": periods.get(a.user_id, [])})
            person_flagged = person_flagged or row["flagged"]
        people.append(row)
    unattributed = expected_by.get(None, Decimal(0)) if assigned else Decimal(0)

    status = _status(closed, missing, variance, limit)
    return {
        "currency": currency, "expected": str(expected.quantize(Q2)), "submitted_total": str(total.quantize(Q2)), "variance": str(variance.quantize(Q2)),
        "cash_difference": str(variance.quantize(Q2)), "variance_pct": str(pct), "status": status,
        "flagged": closed and (status != "ok" or person_flagged), "missing_rate": sorted(set(missing)), "by_payment_type": lines,
        "allowed_variance": str(limit.quantize(Q2)), "per_attendant_expected": assigned, "by_attendant": people,
        **({"unassigned_expected": str(unattributed.quantize(Q2))} if unattributed else {}),
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
