"""Phase 5: do the numbers add up?

Tank:  opening + deliveries - dispensed - closing = unexplained loss (litres). Dispensed comes from the attendants' pump
       meters; dips and deliveries come from managers. Different people, so a mismatch is a stronger signal.
Cash:  what was handed in (all payment types, all currencies, converted at the owner's rate) vs litres x price.
       With several attendants, each person's hand-in is shown beside the total; on assigned-pump stations (and
       whenever one person ran the shift) each person also has their own expected cash. `by_pump` shows the litres
       and sales of every pump and fuel, with who worked it, so a manager can follow each attendant's numbers.
Wording is neutral on purpose: a "cash difference", never an accusation.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Q, Sum

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


def _people(shift):
    """One line per person on the shift. Someone who worked two parts of it counts once: first in, last out, and
    handed in only when every part is."""
    people = {}
    for a in shift.attendants.select_related("user").order_by("joined_at"):
        p = people.get(a.user_id)
        if p is None:
            people[a.user_id] = SimpleNamespace(user_id=a.user_id, user=a.user, is_lead=a.is_lead, joined_at=a.joined_at, left_at=a.left_at,
                                                cash_submitted_at=a.cash_submitted_at)
        else:
            p.is_lead = p.is_lead or a.is_lead
            p.left_at = a.left_at
            p.cash_submitted_at = a.cash_submitted_at if p.cash_submitted_at else None
    return list(people.values())


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

    # per attendant: always what each person handed in; expected per person when pumps are assigned, or when one
    # person ran the whole shift (then all of it is theirs)
    on_shift = _people(shift)
    solo = on_shift[0].user_id if len(on_shift) == 1 else None
    names = {a.user_id: a.user.full_name for a in on_shift}
    assigned = shift.assignments.exists()
    expected_by = {}
    if assigned:
        for row in shift.transactions.values("attendant_id").annotate(s=Sum("amount")).order_by():
            expected_by[row["attendant_id"]] = row["s"] or Decimal(0)
    elif solo:
        expected_by[solo] = expected
    per_person = assigned or solo is not None
    by_pump = []
    pump_rows = (shift.transactions.values("attendant_id", "nozzle__pump_id", "nozzle__pump__name", "fuel_type__code")
                 .annotate(litres=Sum("litres"), amount=Sum("amount")).order_by("nozzle__pump__name", "fuel_type__code"))
    for row in pump_rows:
        who = row["attendant_id"] or solo
        by_pump.append({"pump_id": str(row["nozzle__pump_id"]), "pump": row["nozzle__pump__name"], "fuel_code": row["fuel_type__code"],
                        "attendant": str(who) if who else None, "attendant_name": names.get(who, "Shared") if who else "Shared",
                        "litres": str((row["litres"] or Decimal(0)).quantize(Q3)), "amount": str((row["amount"] or Decimal(0)).quantize(Q2))})
    periods = {}
    for a in shift.assignments.select_related("pump"):
        periods.setdefault(a.user_id, []).append({"pump": a.pump.name, "from": _iso(a.started_at), "to": _iso(a.ended_at)})
    people, person_flagged = [], False
    for a in on_shift:
        submitted = by_person.get(a.user_id, Decimal(0))
        row = {"user": str(a.user_id), "name": a.user.full_name, "is_lead": a.is_lead, "joined_at": _iso(a.joined_at), "left_at": _iso(a.left_at),
               "handed_in": a.cash_submitted_at is not None, "submitted": str(submitted.quantize(Q2))}
        if per_person:
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
        "allowed_variance": str(limit.quantize(Q2)), "per_attendant_expected": per_person, "by_attendant": people, "by_pump": by_pump,
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
    dispensed = Transaction.objects.filter(nozzle__tank=tank, occurred_at__gt=opening.reading_at, occurred_at__lte=closing.reading_at).aggregate(s=Sum("litres"))["s"] or Decimal(0)
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


def finished_parts(shift):
    """A shift still running (assigned pumps stay open all day): what each person's finished parts add up to. Ending a
    part closes every stretch on its pumps, so their sales up to their last end are complete, and their cash is what
    they handed in for those parts. Parts still running wait for their end. Same shape as cash_summary for the report."""
    cfg = get_settings(shift.tenant_id)
    currency = shift.station.pricing_currency
    done = {}
    for a in shift.attendants.select_related("user").filter(left_at__isnull=False):
        p = done.setdefault(a.user_id, SimpleNamespace(user=a.user, until=a.left_at, parts=set()))
        p.until = max(p.until, a.left_at)
        p.parts.add(a.pk)
    assigned = shift.assignments.exists()
    people, by_pump = [], []
    for uid, p in done.items():
        mine = shift.transactions.filter(attendant_id=uid, occurred_at__lte=p.until) if assigned else shift.transactions.none()
        expected = mine.aggregate(s=Sum("amount"))["s"] or Decimal(0)
        submitted, missing = Decimal(0), []
        for c in shift.cash.filter(attendant_id=uid).filter(Q(part_id__in=p.parts) | Q(part__isnull=True)):
            rate = rate_at(c.currency, currency, c.created_at)
            if rate is None:
                missing.append(c.currency)
            else:
                submitted += (c.amount * rate).quantize(Q2)
        difference = submitted - expected
        status = _status(True, missing, difference, max(cfg.cash_variance_amount, expected * cfg.cash_variance_pct / 100))
        people.append({"user": str(uid), "name": p.user.full_name, "submitted": str(submitted.quantize(Q2)), "expected": str(expected.quantize(Q2)),
                       "flagged": assigned and status != "ok"})
        rows = mine.values("nozzle__pump_id", "nozzle__pump__name", "fuel_type__code").annotate(litres=Sum("litres"), amount=Sum("amount")).order_by()
        by_pump += [{"pump_id": str(r["nozzle__pump_id"]), "pump": r["nozzle__pump__name"], "fuel_code": r["fuel_type__code"], "attendant": str(uid),
                     "attendant_name": p.user.full_name, "litres": str((r["litres"] or Decimal(0)).quantize(Q3)),
                     "amount": str((r["amount"] or Decimal(0)).quantize(Q2))} for r in rows]
    return {"by_attendant": people, "by_pump": by_pump, "per_attendant_expected": assigned}


def attendant_report(shifts):
    """Per attendant over a period: litres and sales per pump and fuel, what they handed in, what was expected of
    them and the cash difference. Built from closed shifts, so an attendant's mistakes can be followed one by one, and
    from the finished parts of shifts still running. Shifts where several people shared the pumps count their hand-in,
    but no expected cash per person."""
    people = {}

    def person(uid, name):
        return people.setdefault(uid, {"user": uid, "name": name, "shifts": 0, "pumps": {}, "litres": Decimal(0), "sales": Decimal(0),
                                       "handed_in": Decimal(0), "expected": Decimal(0), "shared_shifts": 0, "flagged_shifts": 0})

    for shift in shifts:
        s = cash_summary(shift) if shift.status != "open" else finished_parts(shift)
        for p in s["by_attendant"]:
            row = person(p["user"], p["name"])
            row["shifts"] += 1
            row["handed_in"] += Decimal(p["submitted"])
            if s["per_attendant_expected"]:
                row["expected"] += Decimal(p["expected"])
                row["flagged_shifts"] += 1 if p.get("flagged") else 0
            else:
                row["shared_shifts"] += 1
        for line in s["by_pump"]:
            if line["attendant"] is None:
                continue
            row = person(line["attendant"], line["attendant_name"])
            key = (line["pump"], line["fuel_code"])
            got = row["pumps"].setdefault(key, {"pump": line["pump"], "fuel_code": line["fuel_code"], "litres": Decimal(0), "amount": Decimal(0)})
            got["litres"] += Decimal(line["litres"])
            got["amount"] += Decimal(line["amount"])
            row["litres"] += Decimal(line["litres"])
            row["sales"] += Decimal(line["amount"])
    out = []
    for row in sorted(people.values(), key=lambda r: r["name"]):
        own = row["shifts"] - row["shared_shifts"]
        out.append({
            "user": row["user"], "name": row["name"], "shifts": row["shifts"], "shared_shifts": row["shared_shifts"], "flagged_shifts": row["flagged_shifts"],
            "litres": str(row["litres"].quantize(Q3)), "sales": str(row["sales"].quantize(Q2)), "handed_in": str(row["handed_in"].quantize(Q2)),
            "expected": str(row["expected"].quantize(Q2)) if own else None,
            "cash_difference": str((row["handed_in"] - row["expected"]).quantize(Q2)) if own and not row["shared_shifts"] else None,
            "pumps": [{**p, "litres": str(p["litres"].quantize(Q3)), "amount": str(p["amount"].quantize(Q2))} for p in sorted(row["pumps"].values(), key=lambda p: (p["pump"], p["fuel_code"]))],
        })
    return out
