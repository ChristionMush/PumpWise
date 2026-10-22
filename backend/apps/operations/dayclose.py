"""Closing the trading day at a station (about 21:00): the pumps pause, the manager dips every tank and reads every
pump's meter, all in one entry. That moment ends the day (tradingday.py) for the sales, the fuel check and the daily
report, so the tanks and the meters line up exactly.

* Every tank and every nozzle at the station is needed; a meter lower than its last number is refused (a typo), unless
  the meter went round.
* A pump someone is working at that moment gets a reading on their shift at the close: their stretch is split there,
  so each day gets its own litres. Their part of the shift goes on and their cash is unchanged.
* One close per station and day. Every close is in the audit log.
"""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction as db_tx
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.permissions import require
from apps.audit import services as audit
from apps.stations.models import Nozzle, Tank

from . import services, tradingday
from .models import DayClose, DayCloseReading, PumpMeterReading


def _went_round(last, value, meter_max):
    return last > meter_max * Decimal("0.9") and value < meter_max * Decimal("0.1")


def close_day(*, user, station, dips, meters, at=None, note="", request=None):
    """dips: [{"tank", "litres" | "dip_mm"}] for every tank; meters: [{"nozzle", "reading"}] for every nozzle."""
    require(user, "tank_records", "Only a manager or the owner closes the day.")
    at = at or timezone.now()
    day = tradingday.day_of_close(at)
    done = DayClose.objects.filter(station=station, day=day).first()
    if done is not None:
        raise ValidationError({"detail": f"{day:%d %b} is already closed at {station.name} "
                                         f"({timezone.localtime(done.closed_at):%H:%M}). Ask the owner if it needs correcting."})
    tanks = list(Tank.objects.filter(station=station).select_related("fuel_type").order_by("name"))
    nozzles = list(Nozzle.objects.filter(pump__station=station).select_related("pump", "tank").order_by("pump__name", "number"))
    dip_of = {d["tank"].pk: d for d in dips}
    meter_of = {m["nozzle"].pk: Decimal(str(m["reading"])).quantize(Decimal("0.01")) for m in meters}
    missing = [t.name for t in tanks if t.pk not in dip_of] + [f"{n.pump.name} {n}" for n in nozzles if n.pk not in meter_of]
    if missing:
        raise ValidationError({"detail": "Enter every tank's dip and every pump's meter. Missing: " + ", ".join(missing) + "."})
    if {d["tank"].station_id for d in dips} - {station.pk} or {m["nozzle"].pump.station_id for m in meters} - {station.pk}:
        raise ValidationError({"detail": "A tank or pump is not at this station."})
    problems = []
    for n in nozzles:
        last = PumpMeterReading.objects.filter(nozzle=n, reading_at__lte=at).order_by("-reading_at", "-created_at").first()
        if last and meter_of[n.pk] < last.reading and not _went_round(last.reading, meter_of[n.pk], n.meter_max):
            problems.append(f"{n.pump.name} {n}: {meter_of[n.pk]} is below its last number {last.reading}")
    if problems:
        raise ValidationError({"detail": "Check these meter numbers: " + "; ".join(problems) + "."})

    with db_tx.atomic():
        close = DayClose.objects.create(tenant_id=station.tenant_id, station=station, day=day, closed_at=at, closed_by=user, note=(note or "").strip())
        for t in tanks:
            d = dip_of[t.pk]
            row = services.record_tank_dip(user=user, tank=t, litres=d.get("litres"), dip_mm=d.get("dip_mm"), at=at)
            row.day_close = close
            row.save(update_fields=["day_close", "updated_at"])
        split = set()
        for n in nozzles:
            DayCloseReading.objects.create(tenant_id=station.tenant_id, day_close=close, nozzle=n, reading=meter_of[n.pk])
            working = services.open_pump_assignment(n.pump)
            if working is not None:  # someone is on this pump: their stretch is split at the close
                services._record_reading(working.shift, n, PumpMeterReading.Kind.CHECKPOINT, meter_of[n.pk], at, user)
                split.add(working.shift)
        for shift in split:
            shift.save()
            services.update_sales_so_far(shift)
        from .rollups import refresh_daily

        for d in (day, day + timedelta(days=1)):  # the sales either side of the close
            refresh_daily(station, d)
        audit.log("day_closed", actor=user, obj=close, request=request, changes={
            "station": station.name, "day": day.isoformat(), "at": at.isoformat(),
            "dips": {t.name: str(dip_of[t.pk].get("litres") or dip_of[t.pk].get("dip_mm")) for t in tanks},
            "meters": {f"{n.pump.name} {n}": str(meter_of[n.pk]) for n in nozzles}})
    return close


def close_status(station):
    """What the Day close screen needs: the day to close now and the last closes."""
    now = timezone.now()
    day = tradingday.day_of_close(now)
    current = DayClose.objects.filter(station=station, day=day).select_related("closed_by").first()
    recent = DayClose.objects.filter(station=station).select_related("closed_by")[:7]

    def card(c):
        return {"id": str(c.pk), "day": c.day.isoformat(), "closed_at": c.closed_at, "closed_by": c.closed_by.full_name if c.closed_by_id else None}

    return {"station": str(station.pk), "day": day.isoformat(), "closed": card(current) if current else None, "recent": [card(c) for c in recent]}
