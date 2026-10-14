"""Shift, reading and transaction rules. Used by the REST API and by offline sync (same code path).

Several people can work one shift (PLAN.md section 4):
* the person who starts it is the lead; others join and leave mid-shift (joined_at / left_at);
* meter numbers are per nozzle per shift, entered once by anyone on it. A second person's DIFFERENT number is
  kept beside the first and flagged "readings differ" for the lead or a manager; nothing is silently overwritten;
* each person hands in their own cash; the shift closes once everyone has handed in or been marked as left.
  If the lead asks to close earlier (typical offline: phones sync in any order), the shift closes by itself
  when the last person is done;
* on assigned-pump stations each pump belongs to one person at a time; a swap ends one assignment and starts the
  next at a meter snapshot, so each person's expected cash covers only their own litres.
"""
from decimal import Decimal

from django.db import IntegrityError
from django.db import transaction as db_tx
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import Role, User
from apps.accounts.permissions import can
from apps.audit import services as audit
from apps.stations.models import FuelType, Nozzle, Pump, Station, Tank
from apps.stations.services import litres_from_dip, price_at

from .adapters import AdapterNotAvailable, get_adapter
from .models import CashSubmission, Delivery, PumpAssignment, PumpMeterReading, Shift, ShiftAttendant, TankReading, Transaction

TWO = Decimal("0.01")
THREE = Decimal("0.001")


class Conflict(Exception):
    """An offline entry cannot be applied (e.g. the shift was already reviewed). Kept for a manager to see."""


def adapter_for(data_source):
    try:
        return get_adapter(data_source)
    except AdapterNotAvailable as exc:
        raise ValidationError({"detail": str(exc)})


# ---- people on a shift ---------------------------------------------------------------------------------------

def attendant_row(shift, user):
    return ShiftAttendant.objects.filter(shift=shift, user=user).first() if user else None


def active_attendants(shift):
    return ShiftAttendant.objects.filter(shift=shift, left_at__isnull=True).select_related("user")


def lead_of(shift):
    return ShiftAttendant.objects.filter(shift=shift, is_lead=True).select_related("user").first()


def people_names(shift):
    return [a.user.full_name for a in ShiftAttendant.objects.filter(shift=shift).select_related("user")]


def is_on_shift(user, shift):
    row = attendant_row(shift, user)
    return row is not None and row.left_at is None


def can_manage(user, shift):
    """Managers/owners, or the shift's lead, may close it, assign pumps and resolve differing readings."""
    if can(user, "manage_shift"):
        return True
    row = attendant_row(shift, user)
    return bool(row and row.is_lead and row.left_at is None)


def require_on_shift(user, shift):
    if not (can(user, "manage_shift") or is_on_shift(user, shift)):
        raise PermissionDenied("This is not your shift.")


def require_manage(user, shift):
    if not can_manage(user, shift):
        raise PermissionDenied("Only the shift lead or a manager can do this.")


def _require_open(shift, message="This shift is closed."):
    if shift.status != Shift.Status.OPEN:
        raise Conflict(message)


def _require_not_reviewed(shift):
    if shift.status == Shift.Status.REVIEWED:
        raise Conflict("Shift already reviewed; ask a manager to correct it.")


def _check_attendant_user(user, tenant_id):
    if user is None or user.tenant_id != tenant_id or user.role != Role.ATTENDANT or not user.is_active:
        raise ValidationError({"attendants": "Choose attendants of this business."})
    return user


def _add_person(shift, user, at, *, lead=False):
    row = attendant_row(shift, user)
    if row is None:
        row = ShiftAttendant.objects.create(tenant_id=shift.tenant_id, shift=shift, user=user, is_lead=lead, joined_at=at)
    elif row.left_at is not None:
        row.left_at = None  # rejoining: same row, history in the audit log
        row.save(update_fields=["left_at", "updated_at"])
    return row


def _ensure_lead(shift):
    """If nobody on the shift is lead (the lead left), the earliest person still on it becomes lead."""
    if ShiftAttendant.objects.filter(shift=shift, is_lead=True, left_at__isnull=True).exists():
        return
    nxt = active_attendants(shift).order_by("joined_at").first()
    if nxt:
        ShiftAttendant.objects.filter(shift=shift, is_lead=True).update(is_lead=False)
        nxt.is_lead = True
        nxt.save(update_fields=["is_lead", "updated_at"])


# ---- readings --------------------------------------------------------------------------------------------------

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
    return qs.order_by("-reading_at", "created_at").first()


def _record_reading(shift, nozzle, kind, reading, at, user, photo=None):
    reading = Decimal(str(reading)).quantize(TWO)
    row = adapter_for(nozzle.pump.data_source).record_meter_reading(shift=shift, nozzle=nozzle, kind=kind, reading=reading, at=at, user=user, photo=photo)
    if kind != PumpMeterReading.Kind.CHECKPOINT:
        _mark_differences(shift, nozzle, kind)
    if kind == PumpMeterReading.Kind.START and not any(f.startswith("gap_since_last_shift") for f in row.flags):
        prev = last_end_reading(nozzle, exclude_shift=shift)
        if prev and prev.reading != reading:
            row.flags = [*row.flags, f"gap_since_last_shift:{(reading - prev.reading).quantize(TWO)}"]
            row.save(update_fields=["flags", "updated_at"])
            shift.add_flag(f"gap_since_last_shift:{nozzle.pk}")
    return row


def _mark_differences(shift, nozzle, kind):
    """Flag (or clear) 'readings differ' on a nozzle's start/end numbers when people entered different values."""
    same = list(PumpMeterReading.objects.filter(shift=shift, nozzle=nozzle, kind=kind))
    differ = len({r.reading for r in same}) > 1
    for r in same:
        flagged = "readings_differ" in r.flags
        if differ != flagged:
            r.flags = [*r.flags, "readings_differ"] if differ else [f for f in r.flags if f != "readings_differ"]
            r.save(update_fields=["flags", "updated_at"])
    key = f"readings_differ:{nozzle.pk}:{kind}"
    if differ:
        shift.add_flag(key)
    else:
        shift.flags = [f for f in shift.flags if f != key]


def _record_many(shift, kind, readings, at, user):
    for item in readings:
        if item["nozzle"].pump.station_id != shift.station_id:
            raise ValidationError({"readings": "A pump does not belong to this station."})
        _record_reading(shift, item["nozzle"], kind, item["reading"], at, user, item.get("photo"))


def resolve_readings(*, user, shift, keep, request=None):
    """Lead or manager picks the right number where two people entered different ones. The others are soft-deleted."""
    require_manage(user, shift)
    _require_not_reviewed(shift)
    if keep.shift_id != shift.pk or keep.kind == PumpMeterReading.Kind.CHECKPOINT:
        raise ValidationError({"reading": "Choose a start or end number of this shift."})
    others = PumpMeterReading.objects.filter(shift=shift, nozzle=keep.nozzle_id, kind=keep.kind).exclude(pk=keep.pk)
    dropped = [str(r.reading) for r in others]
    others.update(deleted_at=timezone.now())
    keep.flags = [f for f in keep.flags if f != "readings_differ"]
    keep.save(update_fields=["flags", "updated_at"])
    shift.flags = [f for f in shift.flags if f != f"readings_differ:{keep.nozzle_id}:{keep.kind}"]
    shift.save(update_fields=["flags", "updated_at"])
    audit.log("readings_resolved", actor=user, obj=keep, changes={"kept": str(keep.reading), "dropped": dropped}, request=request)
    if shift.status != Shift.Status.OPEN:
        compute_transactions(shift)
    return keep


# ---- pump assignment -------------------------------------------------------------------------------------------

def station_pumps(station):
    return list(Pump.objects.filter(station=station).prefetch_related("nozzles"))


def previous_assignments(station):
    """The last shift's pump -> attendant map, so starting the next shift is one tap."""
    last = Shift.objects.filter(station=station, assignments__isnull=False).order_by("-started_at").first()
    if last is None:
        return {}
    out = {}
    for a in PumpAssignment.objects.filter(shift=last).order_by("started_at"):
        out[str(a.pump_id)] = str(a.user_id)  # the latest holder wins
    return out


def _open_assignments(shift, mapping, at, by, *, strict):
    pumps = {str(p.pk): p for p in station_pumps(shift.station)}
    users = {}
    for pump_id, user_id in (mapping or {}).items():
        if str(pump_id) not in pumps:
            raise ValidationError({"assignments": "A pump does not belong to this station."})
        if user_id not in users:
            users[user_id] = _check_attendant_user(User.objects.filter(pk=user_id).first(), shift.tenant_id)
        _add_person(shift, users[user_id], at)
        PumpAssignment.objects.create(tenant_id=shift.tenant_id, shift=shift, pump=pumps[str(pump_id)], user=users[user_id], started_at=at, assigned_by=by)
    missing = [p for pid, p in pumps.items() if pid not in {str(k) for k in (mapping or {})}]
    if missing and strict:
        raise ValidationError({"assignments": "Assign every pump before the shift starts: " + ", ".join(p.name for p in missing) + "."})
    for p in missing:
        shift.add_flag(f"pump_unassigned:{p.pk}")


def reassign_pump(*, user, shift, pump, to_user, readings, at=None, request=None):
    """One-tap swap (break, handover): the pump's meter numbers at the swap split the litres between the two people."""
    require_manage(user, shift)
    _require_open(shift)
    if shift.station.assignment_mode != Station.AssignmentMode.ASSIGNED:
        raise ValidationError({"detail": "Pumps are shared at this station."})
    if pump.station_id != shift.station_id:
        raise ValidationError({"pump": "This pump is not at this station."})
    at = at or timezone.now()
    nozzles = {n.pk for n in pump.nozzles.all()}
    given = {item["nozzle"].pk for item in readings}
    if nozzles - given:
        raise ValidationError({"readings": "Enter the meter number of every nozzle on this pump at the swap."})
    to_user = _check_attendant_user(to_user, shift.tenant_id)
    with db_tx.atomic():
        _record_many(shift, PumpMeterReading.Kind.CHECKPOINT, readings, at, user)
        current = PumpAssignment.objects.filter(shift=shift, pump=pump, ended_at__isnull=True).select_related("user").first()
        if current:
            current.ended_at = at
            current.save(update_fields=["ended_at", "updated_at"])
        _add_person(shift, to_user, at)
        PumpAssignment.objects.create(tenant_id=shift.tenant_id, shift=shift, pump=pump, user=to_user, started_at=at, assigned_by=user)
        shift.flags = [f for f in shift.flags if f != f"pump_unassigned:{pump.pk}"]
        shift.save(update_fields=["flags", "updated_at"])
        audit.log("pump_reassigned", actor=user, obj=shift, request=request, changes={
            "pump": pump.name, "from": current.user.full_name if current else None, "to": to_user.full_name, "at": at.isoformat(),
            "meters": {str(i["nozzle"].pk): str(i["reading"]) for i in readings},
        })
    return shift


# ---- starting, joining, leaving --------------------------------------------------------------------------------

def start_shift(*, user, station, readings, shift_id=None, started_at=None, attendants=None, assignments=None, strict=False):
    """Open a shift and record start meter numbers. Idempotent on shift_id (offline retries).

    The person starting is the lead if they are an attendant; a manager starting names the attendants (the first is lead).
    On a several-attendants station that already has an open shift, the starter joins that shift instead (two phones
    that both started offline end up on one shift). Returns the shift; `shift.merged_from` is set when merged.
    """
    started_at = started_at or timezone.now()
    if shift_id:
        existing = Shift.all_objects.filter(pk=shift_id).first()
        if existing:
            if existing.station_id != station.pk or not (can(user, "manage_shift") or attendant_row(existing, user)):
                raise Conflict("Shift id already used.")
            existing.merged_from = None
            return existing
    people = [_check_attendant_user(u, user.tenant_id) for u in (attendants or [])]
    if user.role == Role.ATTENDANT:
        people = [user, *[p for p in people if p.pk != user.pk]]
    if not people:
        raise ValidationError({"attendants": "Choose who works this shift."})
    multiple = station.attendants_mode == Station.AttendantsMode.MULTIPLE
    if len(people) > 1 and not multiple:
        raise ValidationError({"attendants": "This station has one attendant per shift."})

    if multiple:
        open_shift = Shift.objects.filter(station=station, status=Shift.Status.OPEN).order_by("started_at").first()
        if open_shift:
            with db_tx.atomic():
                for p in people:
                    _add_person(open_shift, p, started_at)
                _ensure_lead(open_shift)
                _record_many(open_shift, PumpMeterReading.Kind.START, readings, open_shift.started_at, user)
                open_shift.save()
            open_shift.merged_from = str(shift_id) if shift_id else ""  # not None: this start joined an open shift
            return open_shift

    try:
        with db_tx.atomic():
            kwargs = {"pk": shift_id} if shift_id else {}
            shift = Shift.objects.create(tenant_id=user.tenant_id, station=station, started_at=started_at, **kwargs)
            for i, p in enumerate(people):
                _add_person(shift, p, started_at, lead=i == 0)
            if station.assignment_mode == Station.AssignmentMode.ASSIGNED:
                _open_assignments(shift, assignments, started_at, user, strict=strict)
            _record_many(shift, PumpMeterReading.Kind.START, readings, shift.started_at, user)
            shift.save()
    except IntegrityError:
        # The id exists but is invisible to us (it belongs to another business): never reveal or reuse it.
        raise Conflict("Shift id already used.")
    shift.merged_from = None
    return shift


def join_shift(*, user, shift, who=None, at=None, request=None):
    """Someone arrives mid-shift. On a one-attendant station this is a handover: the current person must leave first."""
    who = who or user
    if who.pk != user.pk:
        require_manage(user, shift)
    _require_open(shift)
    _check_attendant_user(who, shift.tenant_id)
    at = at or timezone.now()
    if shift.station.attendants_mode != Station.AttendantsMode.MULTIPLE and active_attendants(shift).exclude(user=who).exists():
        raise ValidationError({"detail": "This station has one attendant per shift. The current attendant leaves first (handover)."})
    with db_tx.atomic():
        row = _add_person(shift, who, at)
        _ensure_lead(shift)
        audit.log("shift_joined", actor=user, obj=shift, changes={"who": who.full_name, "at": at.isoformat()}, request=request)
    return row


def leave_shift(*, user, shift, who=None, at=None, request=None):
    """Someone leaves mid-shift (or a manager marks them as left so the shift can close)."""
    who = who or user
    if who.pk != user.pk:
        require_manage(user, shift)
    _require_not_reviewed(shift)
    row = attendant_row(shift, who)
    if row is None:
        raise ValidationError({"detail": "This person is not on the shift."})
    at = at or timezone.now()
    with db_tx.atomic():
        if row.left_at is None:
            row.left_at = at
            row.save(update_fields=["left_at", "updated_at"])
            for a in PumpAssignment.objects.filter(shift=shift, user=who, ended_at__isnull=True):
                a.ended_at = at
                a.save(update_fields=["ended_at", "updated_at"])
                shift.add_flag(f"pump_unassigned:{a.pump_id}")
            shift.save(update_fields=["flags", "updated_at"])
            _ensure_lead(shift)
            audit.log("shift_left", actor=user, obj=shift, changes={"who": who.full_name, "at": at.isoformat()}, request=request)
        maybe_close(shift)
    return row


# ---- cash and closing ------------------------------------------------------------------------------------------

def submit_cash(*, user, shift, cash, who=None, at=None, request=None):
    """One person's handover by payment type. Re-sending before review corrects it (the audit log keeps the old amounts)."""
    who = who or user
    if who.pk != user.pk:
        require_manage(user, shift)
    _require_not_reviewed(shift)
    row = attendant_row(shift, who)
    if row is None:
        raise ValidationError({"detail": "This person is not on the shift."})
    changes = {}
    for c in cash:
        amount = Decimal(str(c["amount"])).quantize(TWO)
        obj, created = CashSubmission.objects.get_or_create(
            shift=shift, attendant=who, payment_type=c["payment_type"], currency=c["currency"],
            defaults={"tenant_id": shift.tenant_id, "amount": amount},
        )
        if not created and obj.amount != amount:
            changes[f"{c['payment_type']}:{c['currency']}"] = [str(obj.amount), str(amount)]
            obj.amount = amount
            obj.save(update_fields=["amount", "updated_at"])
    if row.cash_submitted_at is None:
        row.cash_submitted_at = at or timezone.now()
        row.save(update_fields=["cash_submitted_at", "updated_at"])
    if changes:
        audit.log("cash_corrected", actor=user, obj=shift, changes={"who": who.full_name, **changes}, request=request)
    maybe_close(shift)
    return row


def pending_people(shift):
    """People still on the shift who have not handed in (they must hand in or be marked as left before it closes)."""
    return [a for a in active_attendants(shift) if not a.done]


def missing_end_nozzles(shift):
    started = set(PumpMeterReading.objects.filter(shift=shift, kind=PumpMeterReading.Kind.START).values_list("nozzle_id", flat=True))
    ended = set(PumpMeterReading.objects.filter(shift=shift, kind=PumpMeterReading.Kind.END).values_list("nozzle_id", flat=True))
    return started - ended


def request_close(*, user, shift, at=None, strict=False, request=None):
    """Lead or manager closes the shift. If someone has not handed in yet, it closes by itself once they have."""
    require_manage(user, shift)
    _require_not_reviewed(shift)
    if shift.status != Shift.Status.OPEN:
        return shift
    if strict and (missing := missing_end_nozzles(shift)):
        raise ValidationError({"readings": f"Enter the end number for every pump you started ({len(missing)} missing)."})
    shift.close_requested_at = shift.close_requested_at or at or timezone.now()
    shift.closed_by = user
    shift.save(update_fields=["close_requested_at", "closed_by", "updated_at"])
    maybe_close(shift)
    return shift


def maybe_close(shift):
    if shift.status == Shift.Status.OPEN and shift.close_requested_at and not pending_people(shift):
        _close(shift)
    return shift


def _close(shift):
    shift.status = Shift.Status.CLOSED
    shift.ended_at = shift.ended_at or shift.close_requested_at or timezone.now()
    PumpAssignment.objects.filter(shift=shift, ended_at__isnull=True).update(ended_at=shift.ended_at)
    compute_transactions(shift)
    audit.log("shift_closed", actor=shift.closed_by, tenant_id=shift.tenant_id, obj=shift, changes={"flags": shift.flags})
    from .rollups import on_shift_closed  # rollups import this module, so import here

    on_shift_closed(shift)


def end_shift(*, user, shift, readings, cash, ended_at=None, strict=False, close=None, who=None, request=None):
    """'End of my shift' from a phone: end meter numbers (anyone on the shift), the sender's cash, and, for the lead or a
    manager, a request to close. Re-sending corrects an unreviewed shift."""
    _require_not_reviewed(shift)
    require_on_shift(user, shift)
    ended_at = ended_at or timezone.now()
    was_closed = shift.status != Shift.Status.OPEN
    with db_tx.atomic():
        _record_many(shift, PumpMeterReading.Kind.END, readings, ended_at, user)
        shift.save()
        lead = lead_of(shift)
        person = who or (user if attendant_row(shift, user) else (lead.user if lead else None))
        if person is not None and (cash or person.pk == user.pk):
            submit_cash(user=user, shift=shift, cash=cash, who=person, at=ended_at, request=request)
        if was_closed:  # a correction before review: rebuild the numbers
            compute_transactions(shift)
            from .rollups import on_shift_closed

            on_shift_closed(shift)
        elif close if close is not None else can_manage(user, shift):
            request_close(user=user, shift=shift, at=ended_at, strict=strict, request=request)
    shift.refresh_from_db()
    return shift


def add_checkpoint(*, user, shift, nozzle, reading, at=None):
    """Mid-shift meter reading, taken when the price changes so each part of the shift uses the right price."""
    _require_not_reviewed(shift)
    require_on_shift(user, shift)
    row = _record_reading(shift, nozzle, PumpMeterReading.Kind.CHECKPOINT, reading, at or timezone.now(), user)
    shift.save()
    if shift.status == Shift.Status.CLOSED:
        compute_transactions(shift)
    return row


# ---- transactions ----------------------------------------------------------------------------------------------

def _first(rows):
    """Where two people entered different numbers, the first one counts until the lead or a manager resolves it."""
    return sorted(rows, key=lambda r: (r.created_at, str(r.pk)))[0]


def _holder(assignments, pump_id, start, end):
    for a in assignments.get(pump_id, []):
        if a.started_at <= start and (a.ended_at is None or a.ended_at >= end):
            return a.user_id
    return None


def compute_transactions(shift):
    """(Re)build the shift's transactions from its readings. Old rows are soft-deleted, never destroyed."""
    now = timezone.now()
    Transaction.objects.filter(shift=shift).update(deleted_at=now)
    station = shift.station
    assigned = station.assignment_mode == Station.AssignmentMode.ASSIGNED or PumpAssignment.objects.filter(shift=shift).exists()
    assignments = {}
    for a in PumpAssignment.objects.filter(shift=shift).order_by("started_at"):
        assignments.setdefault(a.pump_id, []).append(a)
    by_nozzle = {}
    for r in PumpMeterReading.objects.filter(shift=shift).select_related("nozzle__pump__tank__fuel_type"):
        by_nozzle.setdefault(r.nozzle_id, {"start": [], "checkpoint": [], "end": []})[r.kind].append(r)
    shift.flags = [f for f in shift.flags if not f.startswith(("no_price", "missing_end", "meter_", "unassigned_period"))]
    created = []
    for rows in by_nozzle.values():
        if not rows["start"]:
            continue
        if not rows["end"]:
            shift.add_flag(f"missing_end:{rows['start'][0].nozzle_id}")
            continue
        checkpoints, seen = [], set()
        for r in sorted(rows["checkpoint"], key=lambda r: (r.reading_at, r.created_at)):
            if r.reading_at not in seen:  # two people snapshotting the same moment count once
                seen.add(r.reading_at)
                checkpoints.append(r)
        chain = [_first(rows["start"]), *checkpoints, _first(rows["end"])]
        nozzle = chain[0].nozzle
        fuel_type = nozzle.pump.tank.fuel_type
        for a, b in zip(chain, chain[1:], strict=False):
            flags = []
            litres = dispensed_litres(a.reading, b.reading, nozzle.meter_max, flags)
            for f in flags:
                shift.add_flag(f"{f}:{nozzle.pk}")
            price = price_at(station.pk, fuel_type.pk, station.pricing_currency, a.reading_at)
            if price is None:
                shift.add_flag(f"no_price:{fuel_type.code}")
            holder = _holder(assignments, nozzle.pump_id, a.reading_at, b.reading_at) if assigned else None
            if assigned and holder is None and litres > 0:
                shift.add_flag(f"unassigned_period:{nozzle.pump_id}")
            per_litre = price.price_per_litre if price else Decimal(0)
            created.append(Transaction.objects.create(
                tenant_id=shift.tenant_id, station=station, shift=shift, nozzle=nozzle, fuel_type=fuel_type, attendant_id=holder,
                litres=litres.quantize(THREE), price_per_litre=per_litre, currency=station.pricing_currency,
                amount=(litres * per_litre).quantize(TWO), started_at=a.reading_at, occurred_at=b.reading_at,
                source=b.source, start_reading=a.reading, end_reading=b.reading,
            ))
    shift.save()
    return created


# ---- tank side (managers and owners only; enforced in the views and sync) ----------------------------------------

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
    row = adapter_for(tank.data_source).record_tank_reading(tank=tank, litres=litres, dip_mm=dip_mm, at=at, user=user, shift=shift, photo=photo, client_id=client_id)
    from .rollups import on_tank_changed

    on_tank_changed(tank)
    return row


def record_delivery(*, user, tank, litres, delivered_at=None, supplier="", note_number="", client_id=None):
    """Fuel received. Idempotent on the phone's client_id (rule 8)."""
    if client_id and (existing := Delivery.objects.filter(client_id=client_id).first()):
        return existing
    row = Delivery.objects.create(
        tenant_id=tank.tenant_id, client_id=client_id, tank=tank, litres_received=Decimal(str(litres)).quantize(THREE),
        delivered_at=delivered_at or timezone.now(), supplier=supplier, note_number=note_number, recorded_by=user,
    )
    from .rollups import on_tank_changed

    on_tank_changed(tank)
    return row


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
