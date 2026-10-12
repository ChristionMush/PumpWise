"""Phase 4: daily sales and usage averages. All amounts come from Transactions, which were priced at the price in force."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.operations.models import Transaction

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
Q2, Q3 = Decimal("0.01"), Decimal("0.001")


def local_day_bounds(day: date):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, time.min), tz)
    return start, start + timedelta(days=1)


def _dec(v, q):
    return str((v or Decimal(0)).quantize(q))


def daily_sales(*, station_ids=None, date_from: date, date_to: date, tank=None):
    start, _ = local_day_bounds(date_from)
    _, end = local_day_bounds(date_to)
    qs = Transaction.objects.filter(occurred_at__gte=start, occurred_at__lt=end)
    if station_ids is not None:
        qs = qs.filter(station_id__in=station_ids)
    if tank is not None:
        qs = qs.filter(nozzle__pump__tank=tank)
    rows = (
        qs.annotate(day=TruncDate("occurred_at"))
        .values("day", "fuel_type__code", "nozzle__pump_id", "nozzle__pump__name", "currency")
        .annotate(litres=Sum("litres"), amount=Sum("amount"))
        .order_by("day")
    )
    days = defaultdict(lambda: {"litres": Decimal(0), "amount": defaultdict(Decimal), "by_fuel": defaultdict(lambda: [Decimal(0), Decimal(0)]), "by_pump": defaultdict(lambda: [Decimal(0), Decimal(0), ""])})
    for r in rows:
        d = days[r["day"]]
        d["litres"] += r["litres"]
        d["amount"][r["currency"]] += r["amount"]
        f = d["by_fuel"][(r["fuel_type__code"], r["currency"])]
        f[0] += r["litres"]; f[1] += r["amount"]
        p = d["by_pump"][(str(r["nozzle__pump_id"]), r["currency"])]
        p[0] += r["litres"]; p[1] += r["amount"]; p[2] = r["nozzle__pump__name"]

    # include quiet days so averages are honest
    last = min(date_to, timezone.localdate())
    out, total_litres, total_amount = [], Decimal(0), defaultdict(Decimal)
    litres_by_day, litres_by_fuel_day = {}, defaultdict(dict)
    n = date_from
    while n <= last:
        d = days.get(n)
        litres = d["litres"] if d else Decimal(0)
        litres_by_day[n] = litres
        total_litres += litres
        entry = {"date": n.isoformat(), "litres": _dec(litres, Q3), "amount_by_currency": {}, "by_fuel": [], "by_pump": []}
        if d:
            for c, a in d["amount"].items():
                entry["amount_by_currency"][c] = _dec(a, Q2); total_amount[c] += a
            for (code, cur), (l, a) in sorted(d["by_fuel"].items()):
                entry["by_fuel"].append({"fuel_code": code, "currency": cur, "litres": _dec(l, Q3), "amount": _dec(a, Q2)})
                litres_by_fuel_day[code][n] = litres_by_fuel_day[code].get(n, Decimal(0)) + l
            for (pid, cur), (l, a, name) in sorted(d["by_pump"].items(), key=lambda kv: kv[1][2]):
                entry["by_pump"].append({"pump_id": pid, "pump_name": name, "currency": cur, "litres": _dec(l, Q3), "amount": _dec(a, Q2)})
        out.append(entry)
        n += timedelta(days=1)

    def averages(series):
        by_wd = defaultdict(list)
        for day, litres in series.items():
            by_wd[day.weekday()].append(litres)
        return [{"weekday": i, "name": WEEKDAYS[i], "days": len(by_wd[i]), "avg_litres": _dec(sum(by_wd[i], Decimal(0)) / len(by_wd[i]) if by_wd[i] else Decimal(0), Q3)} for i in range(7)]

    span = max(len(litres_by_day), 1)
    by_fuel = {}
    for code, series in litres_by_fuel_day.items():
        full = {day: series.get(day, Decimal(0)) for day in litres_by_day}
        by_fuel[code] = {"avg_daily_litres": _dec(sum(full.values(), Decimal(0)) / span, Q3), "by_weekday": averages(full)}
    return {
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "days": out,
        "totals": {"litres": _dec(total_litres, Q3), "amount_by_currency": {c: _dec(a, Q2) for c, a in total_amount.items()}},
        "averages": {"avg_daily_litres": _dec(total_litres / span, Q3), "by_weekday": averages(litres_by_day), "by_fuel": by_fuel},
    }


def tank_daily_litres(tank, days=28, until=None):
    """{date: litres dispensed} for the last `days` local days, quiet days as zero. Feeds the refill forecast."""
    until = until or timezone.localdate()
    data = daily_sales(date_from=until - timedelta(days=days - 1), date_to=until, tank=tank)
    return {date.fromisoformat(d["date"]): Decimal(d["litres"]) for d in data["days"]}
