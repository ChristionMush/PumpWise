"""Closing the trading day at a station (about 21:00): the pumps pause, the manager dips every tank and reads every
pump's meter, all in one entry. That moment ends the day (tradingday.py) for the sales, the fuel check and the daily
report, so the tanks and the meters line up exactly.

* Every tank and every nozzle at the station is needed; a meter lower than its last number is refused (a typo), unless
  the meter went round.
* A pump someone was working at that moment gets a reading on their shift at the close: their stretch is split there,
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
from .models import DayClose, DayCloseReading, PumpMeterReading, Shift


def _went_round(last, value, meter_max):
    return last > meter_max * Decimal("0.9") and value < meter_max * Decimal("0.1")


def working_at(nozzle, at):
    """The shift pumping this nozzle at that moment: it read the nozzle before, and read it again after (or is still
    open, and on an assigned station someone is still on the pump)."""
    starts = (PumpMeterReading.objects.filter(nozzle=nozzle, kind=PumpMeterReading.Kind.START, reading_at__lt=at)
              .select_related("shift__station").order_by("-reading_at")[:5])
    for r in starts:
        shift = r.shift
        if PumpMeterReading.objects.filter(shift=shift, nozzle=nozzle, reading_at__gt=at).exists():
            return shift
        if shift.status == Shift.Status.OPEN:
            if not services.is_assigned_station(shift.station):
                return shift
            working = services.open_pump_assignment(nozzle.pump)
            return shift if working is not None and working.shift_id == shift.pk else None
    return None


def close_day(*, user, station, dips, meters, at=None, note="", request=None):
    """dips: [{"tank", "litres" | "dip_mm"}] for every tank; meters: [{"nozzle", "reading"}] for every nozzle.
    at: when the close was done; an earlier day can be closed afterwards (its sales are re-split at that moment)."""
    require(user, "tank_records", "Only a manager or the owner closes the day.")
    now = timezone.now()
    at = at or now
    if at > now + timedelta(minutes=5):
        raise ValidationError({"detail": "A day can't be closed in the future."})
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
        nxt = PumpMeterReading.objects.filter(nozzle=n, reading_at__gt=at).order_by("reading_at", "created_at").first()
        if nxt and meter_of[n.pk] > nxt.reading and not _went_round(meter_of[n.pk], nxt.reading, n.meter_max):  # closing an earlier day
            problems.append(f"{n.pump.name} {n}: {meter_of[n.pk]} is above the next number {nxt.reading} "
                            f"({timezone.localtime(nxt.reading_at):%d %b %H:%M})")
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
            shift = working_at(n, at)
            if shift is not None:  # someone was pumping through the close: their stretch is split there
                services._record_reading(shift, n, PumpMeterReading.Kind.CHECKPOINT, meter_of[n.pk], at, user)
                split.add(shift)
        for shift in split:
            shift.save()
            if shift.status == Shift.Status.OPEN:
                services.update_sales_so_far(shift)
            else:  # an earlier day closed afterwards: rebuild the shift's sales, same litres and money, split at the close
                services.compute_transactions(shift)
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
