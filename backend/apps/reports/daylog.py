"""Day log (owner): what happened at a station during one trading day, in plain words and in time order, so the owner
can read the story behind the daily report: pumps given out, meter numbers entered, cash handed in and changed, undo
requests and approvals, dips, deliveries, prices and the day close. Points worth a look go at the top.

Built from the records themselves (meter readings, cash) and the audit trail (who changed what). Read-only.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.operations import tradingday
from apps.operations.approvals import LOG_ACTIONS
from apps.operations.models import CashSubmission, DayClose, DayCloseReading, Delivery, PumpMeterReading, Shift, ShiftAttendant, TankReading
from apps.stations.models import FuelPrice, Pump

from .daily import PAYMENT
from .pumps import pump_report

KIND = {"start": "started", "checkpoint": "closed (handover)", "end": "closed (end of shift)"}


def _num(v):
    return f"{Decimal(str(v)):,.2f}"


def _pay(key):
    kind, _, currency = key.partition(":")
    return f"{PAYMENT.get(kind, kind)} {currency}".strip()


def _audit_text(action, c, pumps):
    """One audit row in words, or None for rows the meter and cash lines already tell."""
    who = c.get("who")
    if action in ("pump_assigned", "pump_reassigned"):
        was = f" (was {c['from']})" if c.get("from") and c.get("from") != c.get("to") else ""
        if c.get("swap"):
            return f"Swapped {c.get('pump')} to {c.get('to')} now{was}, at the meter numbers taken then"
        return f"Gave {c.get('pump')} to {c.get('to')}{was}" if c.get("to") else f"Took {c.get('pump')} off {c.get('from')}"
    if action == "cash_corrected":
        moves = [f"{_pay(k)} {v[0] or '0'} → {v[1]}" for k, v in c.items() if k != "who" and isinstance(v, list)]
        return f"Cash changed for {who}: " + ", ".join(moves)
    if action == "shift_end_fixed" and c.get("meters"):
        moves = [f"{pumps.get(n, 'a pump')} {_num(v[0])} → {_num(v[1])}" for n, v in c["meters"].items()]
        return f"{who}'s closing numbers changed: " + ", ".join(moves)
    if action in LOG_ACTIONS:
        return LOG_ACTIONS[action](c)
    if action == "start_reading_carried":
        return f"{c.get('who') or 'The next person'}'s start on {c.get('pump')} moved from {_num(c['from'])} to {_num(c['to'])} (a closing number before it was corrected)"
    if action in ("start_reading_corrected", "meters_added"):
        return c.get("note") and f"{c.get('who')}, {c.get('pump')}: {c['note']}"
    if action == "day_closed":
        dips = ", ".join(f"{t} {Decimal(v):,.0f} L" for t, v in (c.get("dips") or {}).items())
        meters = ", ".join(f"{m} {_num(v)}" for m, v in (c.get("meters") or {}).items())
        return f"Closed the day. Dips: {dips or 'none'}. Meters: {meters or 'none'}"
    if action == "day_close_corrected":
        return f"Corrected the day close of {c.get('day')}"
    if action == "tank_dip_recorded":
        return f"Dipped {c.get('tank')}: {Decimal(c['litres']):,.0f} L"
    if action == "delivery_recorded":
        return f"Delivery into {c.get('tank')}: {Decimal(c['litres']):,.0f} L"
    if action == "price_set":
        return f"Price of {c.get('fuel')} set to {c.get('currency')} {c.get('price')}"
    return None


def day_log(station, day):
    start, end = tradingday.bounds(station.pk, day)
    shifts = list(Shift.objects.filter(station=station, started_at__lte=end).exclude(ended_at__lt=start))
    pumps = {p.pk: p for p in Pump.objects.filter(station=station).prefetch_related("nozzles")}
    nozzle_name = {}
    for p in pumps.values():
        nz = list(p.nozzles.all())
        for n in nz:
            nozzle_name[str(n.pk)] = p.name if len(nz) == 1 else f"{p.name} nozzle {n.number}"
    events, highlights = [], []

    # meter numbers, one line per person and moment
    readings = (PumpMeterReading.objects.filter(shift__in=shifts, reading_at__gt=start, reading_at__lte=end)
                .select_related("recorded_by").order_by("reading_at", "created_at"))
    grouped = {}
    for r in readings:
        grouped.setdefault((r.recorded_by_id, r.kind, r.reading_at), []).append(r)
    for (_, kind, at), rows in grouped.items():
        who = rows[0].recorded_by.full_name if rows[0].recorded_by_id else "Someone"
        parts = []
        for r in rows:
            text = f"{nozzle_name.get(str(r.nozzle_id), 'a pump')} {_num(r.reading)}"
            for f in r.flags:
                if f.startswith("start_from_last_closing:"):
                    text += f" (typed {_num(f.split(':', 1)[1])}; set to the last closing number)"
                if f.startswith("gap_since_last_shift"):
                    text += " (does not match the last closing number)"
                    highlights.append(f"{who} started {nozzle_name.get(str(r.nozzle_id), 'a pump')} at {_num(r.reading)}, which does not match the last closing number.")
            parts.append(text)
            if kind == "start":
                # the day close read the pump higher than this start: litres were sold on it with nobody on it
                later = (DayCloseReading.objects.filter(nozzle_id=r.nozzle_id, day_close__closed_at__lt=at, reading__gt=r.reading)
                         .select_related("day_close").order_by("-day_close__closed_at").first())
                if later is not None:
                    highlights.append(f"{who} started {nozzle_name.get(str(r.nozzle_id), 'a pump')} at {_num(r.reading)}, but the day close at "
                                      f"{timezone.localtime(later.day_close.closed_at):%a %H:%M} already read {_num(later.reading)}: "
                                      f"{later.reading - r.reading:,.2f} L were sold on it before anyone started on it.")
        events.append({"at": at, "by": who, "kind": "meters", "text": f"{who} {KIND.get(kind, kind)}: " + ", ".join(parts)})

    # cash as it stands now, one line per hand-in, at the time it was handed in (a phone may sync it later)
    parts = ShiftAttendant.objects.filter(shift__in=shifts, cash_submitted_at__gt=start, cash_submitted_at__lte=end).select_related("user")
    for p in parts:
        subs = CashSubmission.objects.filter(shift_id=p.shift_id).filter(Q(part=p) | Q(part__isnull=True, attendant=p.user))
        lines = [f"{PAYMENT.get(c.payment_type, c.payment_type)} {c.currency} {_num(c.amount)}" for c in subs if c.amount]
        events.append({"at": p.cash_submitted_at, "by": p.user.full_name, "kind": "cash", "text": f"{p.user.full_name} handed in " + (", ".join(lines) or "nothing")})

    # who changed what (audit trail)
    ids = {str(s.pk) for s in shifts} | {str(p) for p in pumps} | {str(r.pk) for r in readings}
    ids |= {str(x) for x in DayClose.objects.filter(station=station).values_list("pk", flat=True)}
    ids |= {str(x) for x in TankReading.objects.filter(tank__station=station, reading_at__gt=start - timedelta(days=1)).values_list("pk", flat=True)}
    ids |= {str(x) for x in Delivery.objects.filter(tank__station=station, delivered_at__gt=start - timedelta(days=1)).values_list("pk", flat=True)}
    ids |= {str(x) for x in FuelPrice.objects.filter(station=station, created_at__gt=start - timedelta(days=1)).values_list("pk", flat=True)}
    for a in AuditLog.objects.filter(object_id__in=ids, created_at__gt=start, created_at__lte=end).select_related("actor").order_by("created_at"):
        text = _audit_text(a.action, a.changes or {}, nozzle_name)
        if not text:
            continue
        by = a.actor.full_name if a.actor_id and not a.via_platform else "PumpWise"
        events.append({"at": a.created_at, "by": by, "kind": a.action, "text": text})
        if a.action == "cash_corrected":
            for k, v in a.changes.items():
                if isinstance(v, list) and v[0] and v[1] and Decimal(v[1]) < Decimal(v[0]) - 1:
                    highlights.append(f"{a.changes.get('who')}'s {_pay(k)} was lowered from {_num(v[0])} to {_num(v[1])} after handing in.")

    # pumps nobody worked, per shift that overlaps the day
    for st in pump_report([station], day - timedelta(days=1), day):
        for s in st["shifts"]:
            if s["end"] > start and s["start"] < end and s["missing"]:
                highlights.append(f"{s['label']}: {', '.join(s['missing'])} had nobody ({s['covered']}/{s['total']} pumps).")

    events.sort(key=lambda e: e["at"])
    return {"station": str(station.pk), "station_name": station.name, "day": day, "start": start, "end": end, "in_progress": end > timezone.now(),
            "highlights": list(dict.fromkeys(highlights)), "events": events}
