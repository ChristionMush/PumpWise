"""Pump report: for each shift (day 07:00-18:00, night 18:00-07:00, named by the evening it starts), how many of the
station's pumps had an attendant, for example 3/4, who worked each one, and which were left without anyone.

* Assigned pumps: each person's period on a pump (PumpAssignment).
* Shared pumps: everyone on a shift works every pump they started; the pumps with a start number count.

A period counts for a shift when its middle falls in it, or when it covers at least half of it (a long period that runs
through two shifts counts for both).
"""
from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.operations.models import PumpAssignment, PumpMeterReading, Shift, ShiftAttendant
from apps.stations.models import Pump, Station

DAY_START, NIGHT_START = time(7), time(18)


def _slots(date_from, date_to, now):
    """The day and night shifts from date_from to date_to, newest first, none starting after now."""
    out, d = [], date_from
    while d <= date_to:
        for name, a, b in (("Day shift", DAY_START, NIGHT_START), ("Night shift", NIGHT_START, DAY_START)):
            start = timezone.make_aware(datetime.combine(d, a))
            end = timezone.make_aware(datetime.combine(d + timedelta(days=1) if b == DAY_START else d, b))
            if start <= now:
                out.append({"label": f"{name}, {d:%a %d %b}", "start": start, "end": end, "in_progress": end > now})
        d += timedelta(days=1)
    return out[::-1]


def _counts(period_start, period_end, slot):
    overlap = min(period_end, slot["end"]) - max(period_start, slot["start"])
    if overlap <= timedelta(0):
        return False
    middle = period_start + (period_end - period_start) / 2
    return slot["start"] <= middle < slot["end"] or overlap >= (slot["end"] - slot["start"]) / 2


def _periods(station, start, end, now):
    """(pump_id, from, to, person) for everything worked at the station between start and end."""
    if station.assignment_mode == Station.AssignmentMode.ASSIGNED:
        rows = (PumpAssignment.objects.filter(pump__station=station, started_at__lt=end).exclude(ended_at__lte=start)
                .select_related("user"))
        return [(a.pump_id, a.started_at, a.ended_at or now, a.user.full_name) for a in rows]
    out = []
    for s in Shift.objects.filter(station=station, started_at__lt=end).exclude(ended_at__lte=start):
        pumps = set(PumpMeterReading.objects.filter(shift=s, kind=PumpMeterReading.Kind.START).values_list("nozzle__pump_id", flat=True))
        for part in ShiftAttendant.objects.filter(shift=s).select_related("user"):
            to = part.left_at or s.ended_at or now
            out += [(p, part.joined_at, to, part.user.full_name) for p in pumps]
    return out


def pump_report(stations, date_from, date_to, now=None):
    now = now or timezone.now()
    slots = _slots(date_from, date_to, now)
    out = []
    if not slots:
        return out
    for station in stations:
        pumps = list(Pump.objects.filter(station=station).order_by("name"))
        periods = _periods(station, slots[-1]["start"], slots[0]["end"], now)
        rows = []
        for slot in slots:
            people = {p.pk: [] for p in pumps}
            for pump_id, a, b, who in sorted(periods, key=lambda x: x[1]):
                if pump_id in people and _counts(a, b, slot) and who not in people[pump_id]:
                    people[pump_id].append(who)
            covered = sum(1 for p in pumps if people[p.pk])
            rows.append({
                "label": slot["label"], "start": slot["start"], "end": slot["end"], "in_progress": slot["in_progress"],
                "covered": covered, "total": len(pumps),
                "pumps": [{"pump": p.name, "people": people[p.pk]} for p in pumps],
                "missing": [p.name for p in pumps if not people[p.pk]],
            })
        out.append({"station": str(station.pk), "station_name": station.name, "assigned": station.assignment_mode == Station.AssignmentMode.ASSIGNED,
                    "pumps": len(pumps), "shifts": rows})
    return out
