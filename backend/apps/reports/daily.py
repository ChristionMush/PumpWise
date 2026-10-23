"""The daily report for one station and trading day: what the manager used to work out by hand at 21:00.

* Per fuel: litres sold and the money they are worth (each litre at the price in force when it was sold).
* Per pump: the opening meter (the previous day close) and the closing meter (this day close).
* Tanks: the fuel check from the opening dip to the closing dip.
* Grand total: everything handed in during the day, broken down by cash, swipe, EcoCash and intercompany credit
  (intercompany issues are owed by the other company).
* Cash losses: each attendant's shortage on each shift handed in during the day. $1.00 and above is the attendant's
  loss; under $1.00 is a milking error (change, rounding, a drip of fuel), totalled separately.
* Dips: the day close dips in mm and litres, per fuel type.
* Deep dive: each attendant's litres and money within the day, with the shift they belong to.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.operations.models import CashSubmission, DayClose, DayCloseReading, PumpMeterReading, Shift, TankReading, Transaction
from apps.operations.services import dispensed_litres
from apps.operations.tradingday import Calendar
from apps.stations.models import Nozzle, Tank
from apps.tenants.services import get_settings

from .reconciliation import cash_summary, reconcile_tank_day

Q2, Q3 = Decimal("0.01"), Decimal("0.001")
# the grand total's breakdown, always in this order (a type nobody used shows 0.00)
PAYMENT = {"cash": "Cash", "card": "Swipe", "mobile_money": "EcoCash", "credit": "Inter Company Credit"}
MILKING = Decimal("1.00")  # a shortage under $1.00 is a milking error, $1.00 and above the attendant's loss


def _s(v, q):
    return str((v or Decimal(0)).quantize(q))


def shift_checks(station, start, end):
    """The shifts whose cash was handed in during the day, each with its cash check: the day's drill-down."""
    shifts = (Shift.objects.filter(station=station, attendants__cash_submitted_at__gt=start, attendants__cash_submitted_at__lte=end)
              .distinct().select_related("station").order_by("started_at"))
    return [(shift, cash_summary(shift)) for shift in shifts]


def shift_rows(checks):
    """Each shift's cash check. While a shift is open, people still on it have not handed in yet: they are listed as
    waiting (with what they owe so far), and the difference counts only those who have handed in."""
    rows = []
    for s, c in checks:
        waiting, handed_difference = [], Decimal(0)
        if s.status == Shift.Status.OPEN and c["per_attendant_expected"]:
            for a in c["by_attendant"]:
                if a["handed_in"]:
                    handed_difference += Decimal(a["cash_difference"])
                else:
                    waiting.append({"name": a["name"], "expected": a["expected"]})
        rows.append({"shift_id": str(s.pk), "started_at": s.started_at, "ended_at": s.ended_at, "status": s.status,
                     "attendants": [a["name"] for a in c["by_attendant"]], "expected": c["expected"], "handed_in": c["submitted_total"],
                     "cash_difference": c["cash_difference"] if not waiting else _s(handed_difference, Q2), "waiting": waiting,
                     "cash_status": c["status"], "per_attendant_expected": c["per_attendant_expected"],
                     "by_attendant": c["by_attendant"], "by_pump": c["by_pump"]})
    return rows


def cash_losses(checks):
    """Shortages on the shifts whose cash was handed in during the day, per attendant: their own difference when
    pumps are assigned (or they ran the shift alone), else the shift's difference shared by the people on it."""
    people, milking, milking_count = defaultdict(Decimal), Decimal(0), 0
    for shift, c in checks:
        if c["missing_rate"]:
            continue
        if c["per_attendant_expected"]:  # each person who has handed in, even while others work on
            rows = [(a["name"], Decimal(a["cash_difference"])) for a in c["by_attendant"] if a["handed_in"]]
        elif shift.status == Shift.Status.OPEN:  # a shared shift is only short once everyone has handed in
            continue
        else:
            rows = [(" & ".join(a["name"] for a in c["by_attendant"]) or "Not assigned", Decimal(c["variance"]))]
        for name, difference in rows:
            short = -difference
            if short >= MILKING:
                people[name] += short
            elif short > 0:
                milking += short
                milking_count += 1
    by_attendant = [{"attendant": k, "amount": _s(v, Q2)} for k, v in sorted(people.items(), key=lambda kv: -kv[1])]
    attendants = sum(people.values(), Decimal(0))
    return {"total": _s(attendants + milking, Q2), "attendants": _s(attendants, Q2), "by_attendant": by_attendant,
            "milking": _s(milking, Q2), "milking_count": milking_count}


def dips_by_fuel(station, close, end):
    """The day close dips (mm and litres) per fuel type; before the close, each tank's latest dip."""
    out = {}
    for t in Tank.objects.filter(station=station).select_related("fuel_type").order_by("fuel_type__name", "name"):
        qs = TankReading.objects.filter(tank=t)
        dip = (qs.filter(day_close=close) if close else qs.filter(reading_at__lte=end)).order_by("-reading_at").first()
        f = out.setdefault(t.fuel_type.code, {"fuel": t.fuel_type.name, "fuel_code": t.fuel_type.code, "litres": Decimal(0), "tanks": []})
        if dip is not None:
            f["litres"] += dip.litres
        f["tanks"].append({"tank": t.name, "dip_mm": str(dip.dip_mm) if dip and dip.dip_mm is not None else None,
                           "litres": _s(dip.litres, Q3) if dip else None, "at": dip.reading_at if dip else None})
    return [{**f, "litres": _s(f["litres"], Q3)} for f in out.values()]


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

    # per pump: opening and closing meters at the day closes; without a close, the attendants' nearest reading
    # (with its time), so the line is never blank
    opened = {r.nozzle_id: r.reading for r in DayCloseReading.objects.filter(day_close=opening)} if opening else {}
    closed = {r.nozzle_id: r.reading for r in DayCloseReading.objects.filter(day_close=close)} if close else {}
    sold_by_nozzle = {r["nozzle_id"]: r["litres"] for r in sales.values("nozzle_id").annotate(litres=Sum("litres"))}
    until = min(end, timezone.now())

    def attendant_reading(nozzle, at):
        r = PumpMeterReading.objects.filter(nozzle=nozzle, reading_at__lte=at).order_by("-reading_at", "-created_at").first()
        return (r.reading, r.reading_at) if r else (None, None)

    pumps = []
    for n in Nozzle.objects.filter(pump__station=station).select_related("pump", "tank__fuel_type").order_by("pump__name", "number"):
        a, b = opened.get(n.pk), closed.get(n.pk)
        a_at = b_at = None
        if a is None:
            a, a_at = attendant_reading(n, start)
        if b is None:
            b, b_at = attendant_reading(n, until)
        pumps.append({"pump": n.pump.name, "nozzle": str(n), "fuel_code": n.tank.fuel_type.code,
                      "opening_meter": str(a) if a is not None else None, "closing_meter": str(b) if b is not None else None,
                      "opening_from": "close" if n.pk in opened else a_at, "closing_from": "close" if n.pk in closed else b_at,
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
    handed = [{"payment_type": k, "label": label, "amount": _s(cash.get(k), Q2)} for k, label in PAYMENT.items()]
    handed += [{"payment_type": k, "label": k, "amount": _s(v, Q2)} for k, v in cash.items() if k not in PAYMENT]
    received = sum((v for k, v in cash.items() if k != "credit"), Decimal(0))

    # deep dive: each person's litres and money within the day, by shift
    people = [{"attendant": r["attendant__full_name"] or "Not assigned", "shift_id": str(r["shift_id"]), "shift_started_at": r["shift__started_at"],
               "litres": _s(r["litres"], Q3), "amount": _s(r["amount"], Q2)}
              for r in sales.values("attendant__full_name", "shift_id", "shift__started_at").annotate(litres=Sum("litres"), amount=Sum("amount"))
              .order_by("shift__started_at", "attendant__full_name")]

    checks = shift_checks(station, start, end)
    return {
        "station": str(station.pk), "station_name": station.name, "day": day.isoformat(), "currency": station.pricing_currency,
        "from": start, "to": end, "closed": bool(close), "closed_at": close.closed_at if close else None,
        "closed_by": close.closed_by.full_name if close and close.closed_by_id else None, "opened_by_close": bool(opening),
        "so_far": not close and end > timezone.now(),
        "totals": {"litres": _s(totals["litres"], Q3), "amount": _s(totals["amount"], Q2), "handed_in": _s(sum(cash.values(), Decimal(0)), Q2),
                   "grand_total": _s(sum(cash.values(), Decimal(0)), Q2),
                   "received": _s(received, Q2), "intercompany": _s(cash.get("credit"), Q2)},
        "fuels": fuels, "pumps": pumps, "tanks": tanks, "handed_in": handed, "people": people,
        "cash_losses": cash_losses(checks), "dips": dips_by_fuel(station, close, end), "shifts": shift_rows(checks),
    }
