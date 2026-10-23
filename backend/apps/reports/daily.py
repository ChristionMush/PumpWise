"""The daily report for one station and trading day: what the manager used to work out by hand at 21:00.

* Per fuel: litres sold and the money they are worth (each litre at the price in force when it was sold).
* Per pump: the opening meter (the previous day close) and the closing meter (this day close).
* Tanks: the fuel check from the opening dip to the closing dip.
* Grand total: everything handed in during the day, broken down by cash, swipe, EcoCash and intercompany credit
  (intercompany issues are owed by the other company).
* Cash losses: each attendant's shortage on each turn handed in during the day. $1.00 and above is the attendant's
  loss; under $1.00 is a milking error (change, rounding, a drip of fuel), totalled separately.
* Dips: the day close dips in mm and litres, per fuel type.
* Deep dive: each attendant's litres and money within the day, with the shift they belong to.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from apps.operations.approvals import end_summary
from apps.operations.models import (
    CashSubmission,
    DayClose,
    DayCloseReading,
    PumpMeterReading,
    Shift,
    ShiftAttendant,
    TankReading,
    Transaction,
)
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
    """The day's drill-down, built from each attendant's own turn (their meters, what they owe and what they handed
    in), not the whole shift: on assigned pumps a shift can stay open for days while people come and go. A turn
    belongs to the day it was handed in; one whose meters were closed in the day but whose cash is not in yet is
    listed as still to hand in. Grouped by shift."""
    parts = (ShiftAttendant.objects.filter(shift__station=station)
             .filter(Q(cash_submitted_at__gt=start, cash_submitted_at__lte=end) | Q(cash_submitted_at__isnull=True, left_at__gt=start, left_at__lte=end))
             .select_related("shift__station", "user").order_by("joined_at"))
    by_shift = {}
    for part in parts:
        by_shift.setdefault(part.shift, []).append((part, end_summary(part)))
    out = []
    for shift, turns in by_shift.items():
        shared = any(p.cash_submitted_at and e["expected"] is None for p, e in turns)  # pumps not assigned: no per-person amount
        out.append((shift, turns, cash_summary(shift) if shared and shift.status != Shift.Status.OPEN else None))
    return out


def shift_rows(checks):
    """One line per shift for the turns handed in during the day (expected, handed in, difference), and the turns
    still to hand in listed below it."""
    rows = []
    for shift, turns, whole in checks:
        handed = [(p, e) for p, e in turns if p.cash_submitted_at]
        waiting = [{"name": p.user.full_name, "expected": e["expected"]} for p, e in turns if not p.cash_submitted_at]
        if whole is not None:  # shared pumps: the shift's own cash check, once it is closed
            expected, paid, difference = whole["expected"], whole["submitted_total"], whole["cash_difference"]
        elif handed and all(e["expected"] is not None for _, e in handed):
            expected = _s(sum((Decimal(e["expected"]) for _, e in handed), Decimal(0)), Q2)
            paid = _s(sum((Decimal(e["handed_in"]) for _, e in handed), Decimal(0)), Q2)
            difference = _s(Decimal(paid) - Decimal(expected), Q2)
        else:
            expected, difference = None, None
            paid = _s(sum((Decimal(e["handed_in"]) for _, e in handed), Decimal(0)), Q2)
        ends = [p.left_at for p, _ in turns]
        rows.append({"shift_id": str(shift.pk), "started_at": min(p.joined_at for p, _ in turns),
                     "ended_at": None if any(x is None for x in ends) else max(ends), "status": shift.status,
                     "attendants": list(dict.fromkeys(p.user.full_name for p, _ in handed)) or [w["name"] for w in waiting],
                     "expected": expected, "handed_in": paid, "cash_difference": difference, "waiting": waiting,
                     "people": [{"name": p.user.full_name, "expected": e["expected"], "handed_in": e["handed_in"], "cash_difference": e["cash_difference"]}
                                for p, e in handed]})
    return rows


def cash_losses(checks):
    """Shortages on the turns handed in during the day, per attendant: their own difference when pumps are assigned
    (or they worked alone), else the closed shift's difference shared by the people on it."""
    people, milking, milking_count = defaultdict(Decimal), Decimal(0), 0
    for _shift, turns, whole in checks:
        if whole is not None:
            if whole["missing_rate"]:
                continue
            rows = [(" & ".join(a["name"] for a in whole["by_attendant"]) or "Not assigned", Decimal(whole["variance"]))]
        else:
            rows = [(p.user.full_name, Decimal(e["cash_difference"])) for p, e in turns if p.cash_submitted_at and e["cash_difference"] is not None]
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
