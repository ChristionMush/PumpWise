"""Alert rules. `evaluate_tenant` is idempotent: run it as often as you like."""
import json
from datetime import timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from apps.operations.models import Shift, TankReading
from apps.operations.services import people_names
from apps.reports.reconciliation import cash_summary, tank_range
from apps.stations.models import Tank
from apps.tenants.services import get_settings

from .alert_actions import AUTO, AWAITING, OPEN, REOPEN_WITHIN, apply_patterns, log_event
from .forecast import forecast_tank
from .models import Alert

AUTO_RESOLVE = {Alert.Kind.LOW_STOCK, Alert.Kind.MISSED_READING}  # clear themselves when the cause is gone
MISSED_DIP_HOURS = 48
LONG_SHIFT_HOURS = 16
LOOKBACK_DAYS = 3


def evaluate_tenant(tenant_id, now=None):
    now = now or timezone.now()
    cfg = get_settings(tenant_id)
    today = timezone.localtime(now).date()
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
    for s in Shift.objects.filter(status="open", started_at__lt=now - timedelta(hours=LONG_SHIFT_HOURS)).select_related("station"):
        want(f"openshift:{s.pk}", kind="missed_reading", severity="warning", station=s.station, message=f"{s.station.name}: the shift ({', '.join(people_names(s)) or 'no one'}) has been open since {timezone.localtime(s.started_at):%d %b %H:%M}.", data={})

    for row in tank_range(tanks, today - timedelta(days=LOOKBACK_DAYS - 1), today):
        if row["flagged"]:
            t = next(x for x in tanks if str(x.pk) == row["tank_id"])
            what = "unexplained loss" if row["status"] == "loss" else "tank gained fuel it should not have"
            want(f"loss:{t.pk}:{row['date']}", kind="loss", severity="critical" if row["status"] == "loss" else "warning", station=t.station, tank=t,
                 message=f"{t.station.name} {t.name} on {row['date']}: {what} ({row['loss']} L).", data=row)
    for s in Shift.objects.filter(status__in=["closed", "reviewed"], ended_at__gte=now - timedelta(days=LOOKBACK_DAYS)).select_related("station"):
        c = cash_summary(s)
        if c["flagged"]:
            who = [p["name"] for p in c["by_attendant"] if p.get("flagged")] or people_names(s)  # the people it concerns
            want(f"cash:{s.pk}", kind="cash", severity="warning", station=s.station, message=f"{s.station.name}, {', '.join(people_names(s))}: cash difference of {c['variance']} {c['currency']}.",
                 data={**c, "people": who})

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
        if key not in wanted and a.kind in AUTO_RESOLVE and a.status == OPEN:
            a.status, a.resolved_at = AUTO, now
            a.save(update_fields=["status", "resolved_at", "updated_at"])
            log_event(a, "auto_resolved", note="The cause went away (for example a delivery or a dip was recorded).")
            resolved += 1
    apply_patterns(now)
    return {"created": created, "resolved": resolved, "reopened": reopened, "open": Alert.objects.filter(status=OPEN).count()}
