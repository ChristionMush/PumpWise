"""The daily report for one station and trading day: what the manager used to work out by hand at 21:00.

* Per fuel: litres sold and the money they are worth (each litre at the price in force when it was sold).
* Per pump: the opening meter (the previous day close) and the closing meter (this day close).
* Tanks: the fuel check from the opening dip to the closing dip.
* Money handed in during the day, by payment type; intercompany issues are owed by the other company.
* Deep dive: each attendant's litres and money within the day, with the shift they belong to.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.operations.models import CashSubmission, DayClose, DayCloseReading, Transaction
from apps.operations.services import dispensed_litres
from apps.operations.tradingday import Calendar
from apps.stations.models import Nozzle, Tank
from apps.tenants.services import get_settings

from .reconciliation import reconcile_tank_day

Q2, Q3 = Decimal("0.01"), Decimal("0.001")
PAYMENT = {"cash": "Cash", "mobile_money": "EcoCash / mobile money", "card": "Card / swipe", "credit": "Intercompany issue"}


def _s(v, q):
    return str((v or Decimal(0)).quantize(q))


def last_closed_day(station):
    close = DayClose.objects.filter(station=station).order_by("-day").first()
    return close.day if close else None


def daily_report(station, day):
    cal = Calendar(station.pk)
    start, end = cal.bounds(day)
    close = DayClose.objects.filter(station=station, day=day).select_related("closed_by").first()
    opening = DayClose.objects.filter(station=station, day=day - timedelta(days=1)).first()
    sales = Transaction.objects.filter(station=station, occurred_at__gt=start, occurred_at__lte=end)

    # per fuel: litres and money, from the stretches of pumping closed off within the day
    fuels = [{"fuel": r["fuel_type__name"], "fuel_code": r["fuel_type__code"], "litres": _s(r["litres"], Q3), "amount": _s(r["amount"], Q2)}
             for r in sales.values("fuel_type__name", "fuel_type__code").annotate(litres=Sum("litres"), amount=Sum("amount")).order_by("fuel_type__name")]
    totals = sales.aggregate(litres=Sum("litres"), amount=Sum("amount"))

    # per pump: opening and closing meters at the day closes
    opened = {r.nozzle_id: r.reading for r in DayCloseReading.objects.filter(day_close=opening)} if opening else {}
    closed = {r.nozzle_id: r.reading for r in DayCloseReading.objects.filter(day_close=close)} if close else {}
    sold_by_nozzle = {r["nozzle_id"]: r["litres"] for r in sales.values("nozzle_id").annotate(litres=Sum("litres"))}
    pumps = []
    for n in Nozzle.objects.filter(pump__station=station).select_related("pump", "tank__fuel_type").order_by("pump__name", "number"):
        a, b = opened.get(n.pk), closed.get(n.pk)
        pumps.append({"pump": n.pump.name, "nozzle": str(n), "fuel_code": n.tank.fuel_type.code,
                      "opening_meter": str(a) if a is not None else None, "closing_meter": str(b) if b is not None else None,
                      "meter_litres": _s(dispensed_litres(a, b, n.meter_max, []), Q3) if a is not None and b is not None else None,
                      "sold_litres": _s(sold_by_nozzle.get(n.pk), Q3)})

    cfg = get_settings(station.tenant_id)
    tanks = []
    for t in Tank.objects.filter(station=station).select_related("fuel_type").order_by("name"):
        r = reconcile_tank_day(t, day, cfg)
        if r["status"] != "insufficient_data":
            tanks.append({k: r[k] for k in ("tank", "fuel_code", "opening", "deliveries", "dispensed", "closing", "loss", "status")})

    # money handed in during the day (the night shift hands in after the close, so it lands on the next day)
    cash = defaultdict(Decimal)
    for c in CashSubmission.objects.filter(shift__station=station, part__cash_submitted_at__gt=start, part__cash_submitted_at__lte=end):
        cash[c.payment_type] += c.amount
    handed = [{"payment_type": k, "label": PAYMENT.get(k, k), "amount": _s(v, Q2)} for k, v in sorted(cash.items())]
    received = sum((v for k, v in cash.items() if k != "credit"), Decimal(0))

    # deep dive: each person's litres and money within the day, by shift
    people = [{"attendant": r["attendant__full_name"] or "Not assigned", "shift_id": str(r["shift_id"]), "shift_started_at": r["shift__started_at"],
               "litres": _s(r["litres"], Q3), "amount": _s(r["amount"], Q2)}
              for r in sales.values("attendant__full_name", "shift_id", "shift__started_at").annotate(litres=Sum("litres"), amount=Sum("amount"))
              .order_by("shift__started_at", "attendant__full_name")]

    return {
        "station": str(station.pk), "station_name": station.name, "day": day.isoformat(), "currency": station.pricing_currency,
        "from": start, "to": end, "closed": bool(close), "closed_at": close.closed_at if close else None,
        "closed_by": close.closed_by.full_name if close and close.closed_by_id else None, "opened_by_close": bool(opening),
        "so_far": not close and end > timezone.now(),
        "totals": {"litres": _s(totals["litres"], Q3), "amount": _s(totals["amount"], Q2), "handed_in": _s(sum(cash.values(), Decimal(0)), Q2),
                   "received": _s(received, Q2), "intercompany": _s(cash.get("credit"), Q2)},
        "fuels": fuels, "pumps": pumps, "tanks": tanks, "handed_in": handed, "people": people,
    }
