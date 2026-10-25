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
from apps.stations.services import litres_from_dip

from . import services, tradingday
from .models import DayClose, DayCloseReading, PumpMeterReading, Shift, TankReading


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


def latest_close(station):
    return DayClose.objects.filter(station=station).order_by("-day").first()


def correct_close(*, user, close, dips, meters, note="", request=None):
    """Correct the latest day close (a dip on the wrong tank, a typo in a meter). Its dips and meters change, and so
    does the reading taken at the close on the shift of anyone pumping through it; that day's and the next day's
    sales, fuel check and report follow. The same checks as closing apply. Every correction is in the audit log
    and in Dip history."""
    require(user, "tank_records", "Only a manager or the owner corrects a day close.")
    station, at = close.station, close.closed_at
    if latest_close(station).pk != close.pk:
        raise ValidationError({"detail": "Only the latest day close can be corrected."})
    tank_rows = {r.tank_id: r for r in TankReading.objects.filter(day_close=close).select_related("tank")}
    meter_rows = {r.nozzle_id: r for r in DayCloseReading.objects.filter(day_close=close).select_related("nozzle__pump")}
    dip_changes, meter_changes, problems = [], [], []
    for d in dips:
        row = tank_rows.get(d["tank"].pk)
        if row is None:
            raise ValidationError({"detail": f"{d['tank'].name} is not part of this day close."})
        mm = d["dip_mm"]
        litres = Decimal(str(d["litres"])) if d.get("litres") is not None else litres_from_dip(row.tank, mm)
        litres = litres.quantize(Decimal("0.001"))
        if litres < 0 or litres > row.tank.capacity_litres * Decimal("1.05"):
            problems.append(f"{row.tank.name}: {litres} L is more than the tank can hold")
        elif (row.dip_mm, row.litres) != (mm, litres):
            dip_changes.append((row, mm, litres))
    for m in meters:
        row = meter_rows.get(m["nozzle"].pk)
        if row is None:
            raise ValidationError({"detail": "That pump is not part of this day close."})
        n, value = row.nozzle, Decimal(str(m["reading"])).quantize(Decimal("0.01"))
        if value == row.reading:
            continue
        before = PumpMeterReading.objects.filter(nozzle=n, reading_at__lt=at).order_by("-reading_at", "-created_at").first()
        after = PumpMeterReading.objects.filter(nozzle=n, reading_at__gt=at).order_by("reading_at", "created_at").first()
        if before and value < before.reading and not _went_round(before.reading, value, n.meter_max):
            problems.append(f"{n.pump.name} {n}: {value} is below the number before the close, {before.reading}")
        elif after and value > after.reading and not _went_round(value, after.reading, n.meter_max):
            problems.append(f"{n.pump.name} {n}: {value} is above the next number, {after.reading} ({timezone.localtime(after.reading_at):%d %b %H:%M})")
        else:
            meter_changes.append((row, value))
    if problems:
        raise ValidationError({"detail": "Check these numbers: " + "; ".join(problems) + "."})
    if not dip_changes and not meter_changes:
        raise ValidationError({"detail": "Nothing changed."})

    from .rollups import on_tank_changed, refresh_daily

    with db_tx.atomic():
        changes = {"station": station.name, "day": close.day.isoformat(), "closed_at": at.isoformat(), "dips": [], "meters": [], "note": (note or "").strip()}
        for row, mm, litres in dip_changes:
            changes["dips"].append({"tank": row.tank.name, "from": {"dip_mm": _txt(row.dip_mm), "litres": _txt(row.litres)},
                                    "to": {"dip_mm": _txt(mm), "litres": _txt(litres)}})
            row.dip_mm, row.litres = mm, litres
            row.save(update_fields=["dip_mm", "litres", "updated_at"])
            on_tank_changed(row.tank)
        shifts = set()
        for row, value in meter_changes:
            changes["meters"].append({"pump": f"{row.nozzle.pump.name} {row.nozzle}", "from": _txt(row.reading), "to": _txt(value)})
            row.reading = value
            row.save(update_fields=["reading", "updated_at"])
            for cp in PumpMeterReading.objects.filter(nozzle=row.nozzle, reading_at=at, kind=PumpMeterReading.Kind.CHECKPOINT).select_related("shift"):
                cp.reading = value  # the reading on the shift of whoever was pumping through the close
                cp.save(update_fields=["reading", "updated_at"])
                shifts.add(cp.shift)
        for shift in shifts:  # rebuild their sales from the corrected reading (same split at the close)
            services.compute_transactions(shift)
        for d in (close.day, close.day + timedelta(days=1)):
            refresh_daily(station, d)
        audit.log("day_close_corrected", actor=user, obj=close, request=request, changes=changes)
    return close


def _txt(v):
    return format(Decimal(v).normalize(), "f") if v is not None else None


def close_status(station):
    """What the Day close screen needs: the day to close now, the last closes, and the latest close in full (to
    correct it)."""
    now = timezone.now()
    day = tradingday.day_of_close(now)
    current = DayClose.objects.filter(station=station, day=day).select_related("closed_by").first()
    recent = DayClose.objects.filter(station=station).select_related("closed_by")[:7]
    last = latest_close(station)

    def card(c):
        return {"id": str(c.pk), "day": c.day.isoformat(), "closed_at": c.closed_at, "closed_by": c.closed_by.full_name if c.closed_by_id else None}

    latest = None
    if last is not None:
        latest = {**card(last),
                  "dips": [{"tank": str(r.tank_id), "dip_mm": _txt(r.dip_mm), "litres": _txt(r.litres)} for r in TankReading.objects.filter(day_close=last)],
                  "meters": [{"nozzle": str(r.nozzle_id), "reading": _txt(r.reading)} for r in DayCloseReading.objects.filter(day_close=last)]}
    return {"station": str(station.pk), "day": day.isoformat(), "closed": card(current) if current else None, "recent": [card(c) for c in recent],
            "latest": latest}
