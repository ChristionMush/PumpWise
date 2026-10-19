"""Dip history for managers and owners: when each tank was dipped, by whom, and the days a tank was not dipped.

Dips and deliveries are tank-side records (rule 11): attendants never see this.
"""
from datetime import datetime, time, timedelta

from django.utils import timezone

from apps.operations.models import Delivery, TankReading
from apps.stations.models import Tank

# A tank should be dipped at least once every 24 hours. Shown here as a warning; not (yet) enforced anywhere.
DIP_EVERY_HOURS = 24


def _num(value):
    return format(value.normalize(), "f") if value is not None else None  # 10000.000 -> "10000", never "1E+4"


def tank_history(*, station_ids, date_from, date_to, now=None):
    """Per tank: the latest dip and whether it is overdue. Per day (newest first): every dip and delivery in time
    order, and the tanks that had no dip that day."""
    now = now or timezone.now()
    tanks = Tank.objects.select_related("station", "fuel_type").order_by("station__name", "name")
    if station_ids is not None:
        tanks = tanks.filter(station_id__in=station_ids)
    tanks = list(tanks)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end = timezone.make_aware(datetime.combine(date_to + timedelta(days=1), time.min), tz)

    summary = []
    for t in tanks:
        last = TankReading.objects.filter(tank=t).order_by("-reading_at").select_related("recorded_by").first()
        hours = round((now - last.reading_at).total_seconds() / 3600, 1) if last else None
        summary.append({
            "id": str(t.pk), "name": t.name, "station_id": str(t.station_id), "station": t.station.name, "fuel_code": t.fuel_type.code,
            "last_dip_at": last.reading_at.isoformat() if last else None, "last_dip_by": last.recorded_by.full_name if last and last.recorded_by_id else None,
            "hours_since_last_dip": hours, "overdue": hours is None or hours > DIP_EVERY_HOURS,
        })

    by_day = {}
    names = {t.pk: (t.name, t.station.name) for t in tanks}
    dips = TankReading.objects.filter(tank__in=tanks, reading_at__gte=start, reading_at__lt=end).select_related("recorded_by")
    for d in dips:
        day = timezone.localtime(d.reading_at).date()
        by_day.setdefault(day, []).append({
            "type": "dip", "at": d.reading_at.isoformat(), "tank_id": str(d.tank_id), "tank": names[d.tank_id][0], "station": names[d.tank_id][1],
            "dip_mm": _num(d.dip_mm), "litres": _num(d.litres), "by": d.recorded_by.full_name if d.recorded_by_id else None,
        })
    for v in Delivery.objects.filter(tank__in=tanks, delivered_at__gte=start, delivered_at__lt=end).select_related("recorded_by"):
        day = timezone.localtime(v.delivered_at).date()
        by_day.setdefault(day, []).append({
            "type": "delivery", "at": v.delivered_at.isoformat(), "tank_id": str(v.tank_id), "tank": names[v.tank_id][0], "station": names[v.tank_id][1],
            "litres": _num(v.litres_received), "supplier": v.supplier, "note_number": v.note_number, "by": v.recorded_by.full_name if v.recorded_by_id else None,
        })

    days = []
    today = timezone.localdate(now)
    day = min(date_to, today)
    while day >= date_from:
        entries = sorted(by_day.get(day, []), key=lambda e: e["at"])
        dipped = {e["tank_id"] for e in entries if e["type"] == "dip"}
        # a tank that did not exist yet that day is not "missing" a dip
        missing = [f"{t.station.name} · {t.name}" for t in tanks if str(t.pk) not in dipped and timezone.localtime(t.created_at).date() <= day]
        days.append({"date": day.isoformat(), "entries": entries, "missing": missing})
        day -= timedelta(days=1)
    return {"every_hours": DIP_EVERY_HOURS, "tanks": summary, "days": days}
