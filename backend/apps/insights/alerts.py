"""Alert rules. `evaluate_tenant` is idempotent: run it as often as you like."""
import json
from datetime import timedelta
from decimal import Decimal

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q, Sum
from django.utils import timezone

from apps.operations.models import DayCloseReading, Delivery, PumpMeterReading, Shift, ShiftAttendant, TankReading, Transaction
from apps.operations.services import people_names, went_round
from apps.reports.reconciliation import cash_summary
from apps.stations.models import Nozzle, Station, Tank
from apps.tenants.services import get_settings

from .alert_actions import AUTO, AWAITING, OPEN, REOPEN_WITHIN, apply_patterns, log_event
from .forecast import forecast_tank
from .models import Alert

AUTO_RESOLVE = {Alert.Kind.LOW_STOCK, Alert.Kind.MISSED_READING}  # clear themselves when the cause is gone
MISSED_DIP_HOURS = 48
LONG_SHIFT_HOURS = 16
LONG_PART_HOURS = 14  # assigned pumps, one person's part: the longest shift (night, 18:00-07:00) is 13 hours
LOOKBACK_DAYS = 3
CASH_DUE_HOURS = 3  # meters closed at the shift change: the cash is counted and handed in well within this
MISSING_DELIVERY_LITRES = Decimal(500)  # a tank rising this much between two dips with no delivery recorded
DELIVERY_TOLERANCE = (Decimal(100), Decimal(3))  # litres, or percent of the delivery, whichever is more
ASSIGNED = Station.AssignmentMode.ASSIGNED
KIND_WORD = {PumpMeterReading.Kind.START: "start", PumpMeterReading.Kind.CHECKPOINT: "handover", PumpMeterReading.Kind.END: "end"}


def _hhmm(dt):
    return f"{timezone.localtime(dt):%a %d %b %H:%M}"


def _num(v):
    return f"{v:,.2f}"


def _by(reading):
    return reading.recorded_by.full_name if reading.recorded_by_id else "PumpWise"


def meter_problems(since):
    """Meter numbers that cannot all be right, per nozzle, from `since` on: a number below the one before it (the meter
    only goes up; attendants' numbers and the manager's day close are read together), and a pump an incoming attendant
    found at a different number than the handover. Returns ({key: alert kwargs}, every key looked at), so an alert
    clears once its numbers are corrected."""
    found, looked = {}, set()
    for n in Nozzle.objects.select_related("pump__station"):
        station, name = n.pump.station, f"{n.pump.name} {n}"
        rows = list(PumpMeterReading.objects.filter(nozzle=n, reading_at__gte=since).select_related("recorded_by").order_by("reading_at", "created_at"))
        closes = list(DayCloseReading.objects.filter(nozzle=n, day_close__closed_at__gte=since).select_related("day_close"))
        if not rows and not closes:
            continue
        points, times = [], set()
        for r in rows:
            if r.reading_at not in times:  # two people at the same moment: "readings differ" deals with that
                times.add(r.reading_at)
                points.append((r.reading_at, r.reading, f"{_by(r)}'s {KIND_WORD.get(r.kind, r.kind)}"))
        points += [(c.day_close.closed_at, c.reading, "the day close") for c in closes if c.day_close.closed_at not in times]
        points.sort(key=lambda p: p[0])
        before = PumpMeterReading.objects.filter(nozzle=n, reading_at__lt=since).select_related("recorded_by").order_by("-reading_at", "-created_at").first()
        prev = (before.reading_at, before.reading, f"{_by(before)}'s {KIND_WORD.get(before.kind, before.kind)}") if before else None
        for p in points:
            key = f"meter:{n.pk}:{p[0].isoformat()}"
            looked.add(key)
            if prev and p[1] < prev[1] and not went_round(prev[1], p[1], n.meter_max):
                found[key] = dict(kind="meter", severity="warning", station=station, data={"pump": name},
                                  message=f"{station.name} {name}: {p[2]} {_num(p[1])} ({_hhmm(p[0])}) is below {prev[2]} {_num(prev[1])} "
                                          f"({_hhmm(prev[0])}). The meter only goes up: one of them is wrong. Correct it so the litres and cash are right.")
            prev = p
        for r in rows:
            seen = next((f.split(":", 1)[1] for f in r.flags if f.startswith(("pump_showed:", "start_from_last_closing:"))), None)
            if seen is None:
                continue
            key = f"handover:{r.pk}"
            looked.add(key)
            if Decimal(seen) == r.reading:
                continue  # the handover was corrected to what the pump showed
            given = (PumpMeterReading.objects.filter(nozzle=n, reading_at__lte=r.reading_at, kind__in=[PumpMeterReading.Kind.END, PumpMeterReading.Kind.CHECKPOINT])
                     .exclude(pk=r.pk).select_related("recorded_by").order_by("-reading_at", "-created_at").first())
            source = f" ({_by(given)}, {_hhmm(given.reading_at)})" if given else ""
            found[key] = dict(kind="meter", severity="warning", station=station, data={"pump": name, "seen": seen, "handover": str(r.reading)},
                              message=f"{station.name} {name}: {_by(r)} found {_num(Decimal(seen))} on the pump when taking it over ({_hhmm(r.reading_at)}), "
                                      f"but the handover number is {_num(r.reading)}{source}. Check the pump and correct the wrong number.")
    return found, looked


def delivery_problems(since):
    """Between two dips of a tank: fuel came in (the dips rose more than was sold) with no delivery recorded, or a
    delivery that does not match what the dips show. Returns ({key: alert kwargs}, every key looked at)."""
    found, looked = {}, set()
    for t in Tank.objects.select_related("station"):
        dips = list(TankReading.objects.filter(tank=t, reading_at__gte=since).order_by("reading_at"))
        prev = TankReading.objects.filter(tank=t, reading_at__lt=since).order_by("-reading_at").first()
        seq = ([prev] if prev else []) + dips
        for a, b in zip(seq, seq[1:], strict=False):
            key = f"delivery:{t.pk}:{b.pk}"
            looked.add(key)
            delivered = Delivery.objects.filter(tank=t, delivered_at__gt=a.reading_at, delivered_at__lte=b.reading_at).aggregate(s=Sum("litres_received"))["s"] or Decimal(0)
            sold = Transaction.objects.filter(nozzle__tank=t, occurred_at__gt=a.reading_at, occurred_at__lte=b.reading_at).aggregate(s=Sum("litres"))["s"] or Decimal(0)
            came_in = b.litres - a.litres + sold
            between = f"between the dips at {_hhmm(a.reading_at)} and {_hhmm(b.reading_at)}"
            data = {"delivered": str(delivered), "came_in": str(came_in.quantize(Decimal(1))), "from": a.reading_at, "to": b.reading_at}
            if delivered == 0 and came_in > MISSING_DELIVERY_LITRES:
                found[key] = dict(kind="delivery", severity="warning", station=t.station, tank=t, data=data,
                                  message=f"{t.station.name} {t.name}: about {came_in:,.0f} L came in {between}, but no delivery is recorded. Record the delivery.")
            elif delivered > 0 and abs(came_in - delivered) > max(DELIVERY_TOLERANCE[0], delivered * DELIVERY_TOLERANCE[1] / 100):
                found[key] = dict(kind="delivery", severity="warning", station=t.station, tank=t, data=data,
                                  message=f"{t.station.name} {t.name}: {delivered:,.0f} L delivered {between}, but the dips show about {came_in:,.0f} L came in. "
                                          "Check the delivery note and the dips.")
    return found, looked


def evaluate_tenant(tenant_id, now=None):
    now = now or timezone.now()
    cfg = get_settings(tenant_id)
    wanted = {}

    def want(key, **kw):
        kw["data"] = json.loads(json.dumps(kw.get("data", {}), cls=DjangoJSONEncoder))  # datetimes etc. must be JSON-safe
        wanted[key] = kw

    tanks = list(Tank.objects.select_related("fuel_type", "station"))
    for t in tanks:
        f = forecast_tank(t, cfg, now)
        if f["status"] == "ok":
            if float(f["current_litres"]) <= float(t.reserve_litres):
                want(f"low:{t.pk}", kind="low_stock", severity="critical", station=t.station, tank=t, message=f"{t.station.name} {t.name}: at or below the reserve ({f['current_litres']} L left).", data=f)
            elif f["days_to_empty"] is not None and f["days_to_empty"] <= cfg.low_stock_days:
                want(f"low:{t.pk}", kind="low_stock", severity="warning", station=t.station, tank=t, message=f"{t.station.name} {t.name}: about {f['days_to_empty']} days of fuel left. Order by {f['refill_by']}.", data=f)
        last = TankReading.objects.filter(tank=t).order_by("-reading_at").first()
        if last is None or now - last.reading_at > timedelta(hours=MISSED_DIP_HOURS):
            hours = "ever" if last is None else f"for {int((now - last.reading_at).total_seconds() // 3600)} hours"
            want(f"dip:{t.pk}", kind="missed_reading", severity="info", station=t.station, tank=t, message=f"{t.station.name} {t.name}: no dip recorded {hours}.", data={})
    long_shifts = Shift.objects.filter(status="open", started_at__lt=now - timedelta(hours=LONG_SHIFT_HOURS)).exclude(station__assignment_mode=ASSIGNED)
    for s in long_shifts.select_related("station"):
        want(f"openshift:{s.pk}", kind="missed_reading", severity="warning", station=s.station, message=f"{s.station.name}: the shift ({', '.join(people_names(s)) or 'no one'}) has been open since {timezone.localtime(s.started_at):%d %b %H:%M}.", data={})
    # Assigned pumps: the shift stays open all day while people come and go, so look at each person still on it instead.
    # The alert stays until the part is really over: closing the meters is not enough while the cash is still to come.
    due = ShiftAttendant.Approval.CASH_DUE
    long_parts = ShiftAttendant.objects.filter(shift__station__assignment_mode=ASSIGNED).filter(
        Q(shift__status="open", left_at__isnull=True, joined_at__lt=now - timedelta(hours=LONG_PART_HOURS))
        | Q(approval=due, left_at__lt=now - timedelta(hours=CASH_DUE_HOURS)) | Q(approval=due, joined_at__lt=now - timedelta(hours=LONG_PART_HOURS)))
    for a in long_parts.select_related("shift__station", "user"):
        station = a.shift.station
        message = (f"{station.name}: {a.user.full_name} closed their meters at {timezone.localtime(a.left_at):%d %b %H:%M} and has not handed in their cash."
                   if a.left_at else
                   f"{station.name}: {a.user.full_name} has been on shift since {timezone.localtime(a.joined_at):%d %b %H:%M} and has not ended their part.")
        want(f"openpart:{a.pk}", kind="missed_reading", severity="warning", station=station, message=message, data={"person": a.user.full_name})

    # Fuel gains and losses are not alerts: they are tracked as each tank's running difference (Reports, Fuel check, and
    # the accounting pack), where a loss and the matching gain from a dip taken while pumps were in use cancel out.
    # Cash alerts looked at in this run: one that is no longer wanted has cleared (the numbers were corrected).
    checked = set()
    for s in Shift.objects.filter(status__in=["closed", "reviewed"], ended_at__gte=now - timedelta(days=LOOKBACK_DAYS)).select_related("station"):
        if ShiftAttendant.objects.filter(shift=s, approval=ShiftAttendant.Approval.CASH_DUE).exists():
            continue  # someone closed their meters and is still counting their cash: judge the cash once it is in
        checked.add(f"cash:{s.pk}")
        c = cash_summary(s)
        if c["flagged"]:
            who = [p["name"] for p in c["by_attendant"] if p.get("flagged")] or people_names(s)  # the people it concerns
            want(f"cash:{s.pk}", kind="cash", severity="warning", station=s.station, message=f"{s.station.name}, {', '.join(people_names(s))}: cash difference of {c['variance']} {c['currency']}.",
                 data={**c, "people": who})

    # Meter numbers out of order, a handover the incoming attendant did not find on the pump, and deliveries the dips do
    # not agree with. Each clears by itself once its numbers are corrected (its key is looked at and no longer found).
    since = now - timedelta(days=LOOKBACK_DAYS)
    for found, looked in (meter_problems(since), delivery_problems(since)):
        for key, kw in found.items():
            want(key, **kw)
        checked |= looked

    open_alerts = {a.dedupe_key: a for a in Alert.objects.filter(status__in=[OPEN, AWAITING])}
    created = resolved = reopened = 0
    for key, kw in wanted.items():
        if key in open_alerts:
            existing = open_alerts[key]
            if existing.message != kw["message"] or existing.severity != kw["severity"]:
                existing.message, existing.severity, existing.data = kw["message"], kw["severity"], kw["data"]
                existing.save(update_fields=["message", "severity", "data", "updated_at"])
            continue
        last = Alert.objects.filter(dedupe_key=key, status__in=["resolved", AUTO]).order_by("-resolved_at", "-created_at").first()
        if last is not None and last.resolved_by_id is not None:
            # A person resolved it, and their answer stands. A loss or cash difference belongs to one day or one shift and
            # is still in the numbers after it is explained, so it never comes back by itself. A standing condition (low
            # stock, a missing dip) that a person resolved comes back only if it is still there 24 hours later.
            if last.kind not in AUTO_RESOLVE or (last.resolved_at and last.resolved_at >= now - REOPEN_WITHIN):
                continue
        recent = last if last is not None and (last.resolved_by_id is not None or (last.resolved_at and last.resolved_at >= now - REOPEN_WITHIN)) else None
        # the same problem back (closed by itself less than 24 hours ago, or still there a day after a person resolved it):
        # reopen that alert so its story continues, instead of starting a new one
        if recent is not None:
            recent.status, recent.resolved_at, recent.reviewed_at, recent.reviewed_by = OPEN, None, None, None
            recent.reminded_at = recent.escalated_at = None
            recent.reopened_count += 1
            recent.message, recent.severity, recent.data = kw["message"], kw["severity"], kw["data"]
            recent.save()
            log_event(recent, "reopened", note="The problem came back within 24 hours of being closed.")
            reopened += 1
            continue
        alert = Alert.objects.create(tenant_id=tenant_id, dedupe_key=key, **kw)
        log_event(alert, "opened", note=alert.message)
        created += 1
    for key, a in open_alerts.items():
        if a.kind == Alert.Kind.LOSS and a.status == OPEN:
            note = "Fuel gains and losses are no longer alerts: they are tracked as the tank's running difference in Reports, Fuel check."
        elif key not in wanted and a.status == OPEN and (a.kind in AUTO_RESOLVE or key in checked):
            note = ("The numbers were corrected: there is no difference any more." if key in checked
                    else "The cause went away (for example a delivery or a dip was recorded).")
        else:
            continue
        a.status, a.resolved_at, a.resolution_note = AUTO, now, note
        a.save(update_fields=["status", "resolved_at", "resolution_note", "updated_at"])
        log_event(a, "auto_resolved", note=note)
        resolved += 1
    apply_patterns(now)
    return {"created": created, "resolved": resolved, "reopened": reopened, "open": Alert.objects.filter(status=OPEN).count()}
