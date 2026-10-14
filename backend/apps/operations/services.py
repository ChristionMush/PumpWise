"""Shift, reading and transaction rules. Used by the REST API and by offline sync (same code path)."""
from decimal import Decimal

from django.db import IntegrityError
from django.db import transaction as db_tx
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.stations.models import FuelType, Nozzle, Station, Tank
from apps.stations.services import litres_from_dip, price_at

from .adapters import AdapterNotAvailable, get_adapter
from .models import CashSubmission, Delivery, PumpMeterReading, Shift, TankReading, Transaction

TWO = Decimal("0.01")
THREE = Decimal("0.001")


class Conflict(Exception):
    """An offline entry cannot be applied (e.g. the shift was already reviewed). Kept for a manager to see."""


def adapter_for(data_source):
    try:
        return get_adapter(data_source)
    except AdapterNotAvailable as exc:
        raise ValidationError({"detail": str(exc)})


def dispensed_litres(start, end, meter_max, flags):
    """Litres between two meter readings, handling rollover. Backwards readings are flagged, never silently used."""
    if end >= start:
        return end - start
    if start > meter_max * Decimal("0.9") and end < meter_max * Decimal("0.1"):
        flags.append("meter_rollover")
        return meter_max - start + end + Decimal("0.01")  # the step from max back to 0.00 is a real 0.01
    flags.append("meter_went_backwards")
    return Decimal(0)


def last_end_reading(nozzle, exclude_shift=None):
    qs = PumpMeterReading.objects.filter(nozzle=nozzle, kind=PumpMeterReading.Kind.END)
    if exclude_shift is not None:
        qs = qs.exclude(shift=exclude_shift)
    return qs.order_by("-reading_at").first()


def _record_reading(shift, nozzle, kind, reading, at, user, photo=None):
    reading = Decimal(str(reading)).quantize(TWO)
    row = adapter_for(nozzle.pump.data_source).record_meter_reading(shift=shift, nozzle=nozzle, kind=kind, reading=reading, at=at, user=user, photo=photo)
    if kind == PumpMeterReading.Kind.START and not row.flags:
        prev = last_end_reading(nozzle, exclude_shift=shift)
        if prev and prev.reading != reading:
            row.flags = [f"gap_since_last_shift:{(reading - prev.reading).quantize(TWO)}"]
            row.save(update_fields=["flags", "updated_at"])
            shift.add_flag(f"gap_since_last_shift:{nozzle.pk}")
    return row


def start_shift(*, user, station, readings, shift_id=None, started_at=None):
    """Open a shift and record start meter numbers. Idempotent on shift_id (offline retries)."""
    if shift_id:
        existing = Shift.all_objects.filter(pk=shift_id).first()
        if existing:
            if existing.station_id != station.pk or existing.attendant_id != user.pk:
                raise Conflict("Shift id already used.")
            return existing
    try:
        with db_tx.atomic():
            kwargs = {"pk": shift_id} if shift_id else {}
            shift = Shift.objects.create(tenant_id=user.tenant_id, station=station, attendant=user, started_at=started_at or timezone.now(), **kwargs)
            for item in readings:
                _record_reading(shift, item["nozzle"], PumpMeterReading.Kind.START, item["reading"], shift.started_at, user, item.get("photo"))
            shift.save()
    except IntegrityError:
        # The id exists but is invisible to us (it belongs to another business): never reveal or reuse it.
        raise Conflict("Shift id already used.")
    return shift


def add_checkpoint(*, user, shift, nozzle, reading, at=None):
    """Mid-shift meter reading, taken when the price changes so each part of the shift uses the right price."""
    if shift.status == Shift.Status.REVIEWED:
        raise Conflict("Shift already reviewed.")
    row = _record_reading(shift, nozzle, PumpMeterReading.Kind.CHECKPOINT, reading, at or timezone.now(), user)
    if shift.status == Shift.Status.CLOSED:
        compute_transactions(shift)
    return row


def compute_transactions(shift):
    """(Re)build the shift's transactions from its readings. Old rows are soft-deleted, never destroyed."""
    now = timezone.now()
    Transaction.objects.filter(shift=shift).update(deleted_at=now)
    station = shift.station
    rows = {}
    for r in PumpMeterReading.objects.filter(shift=shift).select_related("nozzle__pump__tank").order_by("reading_at", "kind"):
        rows.setdefault(r.nozzle_id, []).append(r)
    shift.flags = [f for f in shift.flags if not f.startswith(("no_price", "missing_end", "meter_"))]
    created = []
    for readings in rows.values():
        readings.sort(key=lambda r: ({"start": 0, "checkpoint": 1, "end": 2}[r.kind], r.reading_at))
        if readings[0].kind != "start":
            continue
        if readings[-1].kind != "end":
            shift.add_flag(f"missing_end:{readings[0].nozzle_id}")
            continue
        nozzle = readings[0].nozzle
        fuel_type = nozzle.pump.tank.fuel_type
        for a, b in zip(readings, readings[1:], strict=False):
            flags = []
            litres = dispensed_litres(a.reading, b.reading, nozzle.meter_max, flags)
            for f in flags:
                shift.add_flag(f"{f}:{nozzle.pk}")
            price = price_at(station.pk, nozzle.pump.tank.fuel_type_id, station.pricing_currency, a.reading_at)
            if price is None:
                shift.add_flag(f"no_price:{fuel_type.code}")
            per_litre = price.price_per_litre if price else Decimal(0)
            created.append(Transaction.objects.create(
                tenant_id=shift.tenant_id, station=station, shift=shift, nozzle=nozzle, fuel_type=fuel_type,
                litres=litres.quantize(THREE), price_per_litre=per_litre, currency=station.pricing_currency,
                amount=(litres * per_litre).quantize(TWO), started_at=a.reading_at, occurred_at=b.reading_at,
                source=b.source, start_reading=a.reading, end_reading=b.reading,
            ))
    shift.save()
    return created


def end_shift(*, user, shift, readings, cash, ended_at=None, strict=False):
    """Close a shift: end meter numbers + what was handed in. Re-sending corrects an unreviewed shift."""
    if shift.status == Shift.Status.REVIEWED:
        raise Conflict("Shift already reviewed; ask a manager to correct it.")
    ended_at = ended_at or timezone.now()
    with db_tx.atomic():
        started = {r.nozzle_id for r in PumpMeterReading.objects.filter(shift=shift, kind=PumpMeterReading.Kind.START)}
        given = {item["nozzle"].pk for item in readings}
        if strict and (missing := started - given - {r.nozzle_id for r in PumpMeterReading.objects.filter(shift=shift, kind="end")}):
            raise ValidationError({"readings": f"Enter the end number for every pump you started ({len(missing)} missing)."})
        for item in readings:
            _record_reading(shift, item["nozzle"], PumpMeterReading.Kind.END, item["reading"], ended_at, user, item.get("photo"))
        for c in cash:
            CashSubmission.objects.update_or_create(
                shift=shift, payment_type=c["payment_type"], currency=c["currency"],
                defaults={"tenant_id": shift.tenant_id, "amount": Decimal(str(c["amount"])).quantize(TWO)},
            )
        shift.ended_at = shift.ended_at or ended_at
        shift.status = Shift.Status.CLOSED
        compute_transactions(shift)
    return shift


def record_tank_dip(*, user, tank, litres=None, dip_mm=None, at=None, shift=None, photo=None, client_id=None):
    """A manager's dip. Idempotent on the phone's client_id (rule 8): a retry returns the row already saved."""
    if client_id and (existing := TankReading.objects.filter(client_id=client_id).first()):
        return existing
    if litres is None and dip_mm is None:
        raise ValidationError({"litres": "Enter the dip height or the litres."})
    if litres is None:
        litres = litres_from_dip(tank, dip_mm)
    litres = Decimal(str(litres)).quantize(THREE)
    if litres < 0 or litres > tank.capacity_litres * Decimal("1.05"):
        raise ValidationError({"litres": "That is more than the tank can hold. Check the number."})
    return adapter_for(tank.data_source).record_tank_reading(tank=tank, litres=litres, dip_mm=dip_mm, at=at, user=user, shift=shift, photo=photo, client_id=client_id)


def record_delivery(*, user, tank, litres, delivered_at=None, supplier="", note_number="", client_id=None):
    """Fuel received. Idempotent on the phone's client_id (rule 8)."""
    if client_id and (existing := Delivery.objects.filter(client_id=client_id).first()):
        return existing
    return Delivery.objects.create(
        tenant_id=tank.tenant_id, client_id=client_id, tank=tank, litres_received=Decimal(str(litres)).quantize(THREE),
        delivered_at=delivered_at or timezone.now(), supplier=supplier, note_number=note_number, recorded_by=user,
    )


def review_shift(*, user, shift, notes=""):
    if shift.status == Shift.Status.OPEN:
        raise ValidationError({"detail": "Close the shift before reviewing it."})
    shift.status, shift.reviewed_by = Shift.Status.REVIEWED, user
    if notes:
        shift.notes = notes
    shift.save()
    return shift


# Re-exported for views that resolve ids
__all__ = ["Nozzle", "Station", "Tank", "FuelType", "TankReading"]
