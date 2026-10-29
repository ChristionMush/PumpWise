"""Shift, reading and transaction rules. Used by the REST API and by offline sync (same code path).

Several people can work one shift (PLAN.md section 4):
* the person who starts it is the lead; others join and leave mid-shift (joined_at / left_at);
* meter numbers are per nozzle per shift, entered once by anyone on it. A second person's DIFFERENT number is
  kept beside the first and flagged "readings differ" for the lead or a manager; nothing is silently overwritten;
* each person hands in their own cash; the shift closes once everyone has handed in or been marked as left.
  If the lead asks to close earlier (typical offline: phones sync in any order), the shift closes by itself
  when the last person is done;
* on assigned-pump stations a manager or owner gives each pump to one attendant (Pump.assigned_to); attendants never
  assign or swap pumps. There is still ONE shift at the station with many attendants on it: each attendant starts on
  their own pumps (the first opens the shift, the next ones join it) and individually ends their part, with the meter
  numbers of their pumps and their own cash. Their pumps then pass to the incoming attendant, whose period starts at
  those numbers. Each pump period (PumpAssignment) is bounded by meter readings, so every litre belongs to exactly one
  person. The shift closes when the last person on it ends their part. A manager can also swap a pump at once.
"""
import contextvars
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError
from django.db import transaction as db_tx
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import Role
from apps.accounts.permissions import can
from apps.audit import services as audit
from apps.stations.models import FuelType, Nozzle, Pump, Station, Tank
from apps.stations.services import litres_from_dip, price_at

from .adapters import AdapterNotAvailable, get_adapter
from .models import CashSubmission, Delivery, PumpAssignment, PumpMeterReading, Shift, ShiftAttendant, TankReading, Transaction, UndoRequest

TWO = Decimal("0.01")
THREE = Decimal("0.001")
APPROVAL = ShiftAttendant.Approval
HELD = (APPROVAL.PENDING, APPROVAL.SENT_BACK)
CASH_DUE = APPROVAL.CASH_DUE  # assigned pumps: meters closed at the shift change, cash counted and handed in after


class Conflict(Exception):
    """An offline entry cannot be applied (e.g. the shift was already reviewed). Kept for a manager to see."""


def adapter_for(data_source):
    try:
        return get_adapter(data_source)
    except AdapterNotAvailable as exc:
        raise ValidationError({"detail": str(exc)})


# ---- people on a shift ---------------------------------------------------------------------------------------

def attendant_row(shift, user):
    """This person's current part of the shift: their latest one (someone can work two parts of one shift)."""
    return ShiftAttendant.objects.filter(shift=shift, user=user).order_by("-joined_at", "-created_at").first() if user else None


def active_attendants(shift):
    return ShiftAttendant.objects.filter(shift=shift, left_at__isnull=True).select_related("user")


def lead_of(shift):
    return ShiftAttendant.objects.filter(shift=shift, is_lead=True).select_related("user").first()


def people_names(shift):
    return list(dict.fromkeys(a.user.full_name for a in ShiftAttendant.objects.filter(shift=shift).select_related("user")))


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
    if row is None or (row.left_at is not None and row.cash_submitted_at is not None):
        # new on the shift, or back for another part after ending one (handed in and approved): a part of its own
        row = ShiftAttendant.objects.create(tenant_id=shift.tenant_id, shift=shift, user=user, is_lead=lead and row is None, joined_at=at)
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


# ---- end of shift approval -------------------------------------------------------------------------------------
# An attendant's end of shift (their meters and cash) waits for a manager. Until it is approved they cannot start or
# join another shift. The manager may send it back to fix. Any other change to what was handed in needs the owner's
# permission first (UndoRequest). Managers and the owner correct directly; the audit log keeps the old numbers.

def held_end(user):
    """This attendant's end of shift still waiting for a manager, or sent back to fix (at any station), if any."""
    return (ShiftAttendant.objects.filter(user=user, approval__in=HELD).select_related("shift__station", "user")
            .order_by("-cash_submitted_at").first())


def cash_due_part(user):
    """This attendant closed their meters but has not handed in their cash yet (at any station), if so."""
    return ShiftAttendant.objects.filter(user=user, approval=CASH_DUE).select_related("shift__station", "user").order_by("-left_at").first()


def require_cleared(person):
    """No new shift for someone whose last end of shift is not approved yet, or whose cash is not handed in yet."""
    due = cash_due_part(person)
    if due is not None:
        raise ValidationError({"detail": f"{person.full_name} closed their meters at {due.shift.station.name} but has not handed in their cash yet. "
                                         "Hand in the cash first."})
    row = held_end(person)
    if row is not None:
        what = "was sent back to fix" if row.approval == APPROVAL.SENT_BACK else "is waiting for the manager's approval"
        raise ValidationError({"detail": f"{person.full_name}'s last shift at {row.shift.station.name} {what}. "
                                         "A new shift can start once a manager approves it."})


def _mark_handed_in(row, user, at):
    """The first hand-in ends this person's part and puts it in front of a manager. A manager or the owner entering it
    for them approves it at the same time."""
    row.cash_submitted_at = at
    if can(user, "manage_shift"):
        row.approval, row.approval_by, row.approval_at = APPROVAL.APPROVED, user, at
    else:
        row.approval, row.approval_by, row.approval_at = APPROVAL.PENDING, None, None
    row.save(update_fields=["cash_submitted_at", "approval", "approval_by", "approval_at", "updated_at"])


def unlock_change(user, row, kind):
    """Something already handed in changes only when a manager or the owner does it, when the manager sent the end
    back to fix, or when the owner allowed an undo of this kind. Returns the owner's permission to use up, if any."""
    if can(user, "manage_shift") or row.approval == APPROVAL.SENT_BACK:
        return None
    grant = UndoRequest.objects.filter(attendant=row, kind=kind, status=UndoRequest.Status.ALLOWED).first()
    if grant is None:
        raise ValidationError({"detail": 'This is already handed in. To change it, tap "Ask to undo": a manager or the owner allows it first.'})
    return grant


def after_change(user, row, grant):
    """An attendant changed their handed-in end: the permission is used up and it goes back to the manager."""
    if grant is not None:
        grant.status, grant.used_at = UndoRequest.Status.USED, timezone.now()
        grant.save(update_fields=["status", "used_at", "updated_at"])
    if not can(user, "manage_shift"):
        row.approval, row.approval_by, row.approval_at = APPROVAL.PENDING, None, None
        row.save(update_fields=["approval", "approval_by", "approval_at", "updated_at"])


def _new_end_numbers(shift, readings):
    """Whether any of these end numbers differs from every end number already on the shift for that nozzle."""
    have = {}
    for r in PumpMeterReading.objects.filter(shift=shift, kind=PumpMeterReading.Kind.END):
        have.setdefault(r.nozzle_id, set()).add(r.reading)
    return any(Decimal(str(i["reading"])).quantize(TWO) not in have.get(i["nozzle"].pk, set()) for i in readings)


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
    """The nozzle's latest closing number: a shift's end, or a handover inside a shift (assigned pumps)."""
    qs = PumpMeterReading.objects.filter(nozzle=nozzle, kind__in=[PumpMeterReading.Kind.END, PumpMeterReading.Kind.CHECKPOINT])
    if exclude_shift is not None:
        qs = qs.exclude(shift=exclude_shift)
    return qs.order_by("-reading_at", "created_at").first()


# Set while an attendant starts from the app (views.do_shift_start): their start numbers come from the pump's last
# closing number. Imports and the demo replay record the numbers they are given.
FROM_LAST_CLOSING = contextvars.ContextVar("from_last_closing", default=False)


def closing_before(nozzle, at):
    """The pump's latest closing number before a moment: an end, a handover or a day close, on any shift."""
    return (PumpMeterReading.objects.filter(nozzle=nozzle, kind__in=[PumpMeterReading.Kind.END, PumpMeterReading.Kind.CHECKPOINT], reading_at__lte=at)
            .order_by("-reading_at", "-created_at").first())


def _record_reading(shift, nozzle, kind, reading, at, user, photo=None):
    reading = Decimal(str(reading)).quantize(TWO)
    typed = None
    if kind == PumpMeterReading.Kind.START and FROM_LAST_CLOSING.get() and not can(user, "manage_shift"):
        # an attendant starts from the last closing number on the pump; they do not type their own (a typo there
        # would charge them, or the person before, for fuel nobody sold). Managers may, for a repaired meter.
        prev = closing_before(nozzle, at)
        # only while the pump is idle since that closing: taking over a pump someone is still on, the number is theirs to give
        idle = prev is not None and not PumpMeterReading.objects.filter(nozzle=nozzle, kind=PumpMeterReading.Kind.START,
                                                                       reading_at__gt=prev.reading_at, reading_at__lte=at).exists()
        if idle and prev.reading != reading:
            typed, reading = reading, prev.reading
    row = adapter_for(nozzle.pump.data_source).record_meter_reading(shift=shift, nozzle=nozzle, kind=kind, reading=reading, at=at, user=user, photo=photo)
    if typed is not None:
        row.flags = [*row.flags, f"start_from_last_closing:{typed}"]  # what they had typed, kept for the record
        row.save(update_fields=["flags", "updated_at"])
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


def carry_to_next_start(*, user, nozzle, old, new, after, request=None):
    """A closing number was corrected (from old to new). Whoever started from the old number on this pump (the next
    start reading after it, if it equals the old number) now starts from the corrected one, even if their shift is
    running or closed; their sales are rebuilt and it is logged. Returns their start reading, or None."""
    old, new = Decimal(str(old)).quantize(TWO), Decimal(str(new)).quantize(TWO)
    if old == new:
        return None
    nxt = (PumpMeterReading.objects.filter(nozzle=nozzle, kind=PumpMeterReading.Kind.START, reading_at__gte=after)
           .select_related("shift__station", "recorded_by").order_by("reading_at", "created_at").first())
    if nxt is None or nxt.reading != old:
        return None  # nobody started from that number (or they entered their own)
    nxt.reading = new
    nxt.flags = [f for f in nxt.flags if not f.startswith("gap_since_last_shift")]
    nxt.save(update_fields=["reading", "flags", "updated_at"])
    shift = nxt.shift
    shift.flags = [f for f in shift.flags if f != f"gap_since_last_shift:{nozzle.pk}"]
    shift.save()
    if shift.status == Shift.Status.OPEN:
        update_sales_so_far(shift)
    else:
        from .rollups import on_shift_closed  # rollups import this module

        compute_transactions(shift)
        on_shift_closed(shift)
    audit.log("start_reading_carried", actor=user, obj=nxt, request=request, changes={
        "pump": f"{nozzle.pump.name} {nozzle}", "from": str(old), "to": str(new),
        "who": nxt.recorded_by.full_name if nxt.recorded_by_id else None})
    return nxt


def resolve_readings(*, user, shift, keep, request=None):
    """Lead or manager picks the right number where two people entered different ones. The others are soft-deleted."""
    require_manage(user, shift)
    _require_not_reviewed(shift)
    if keep.shift_id != shift.pk or keep.kind == PumpMeterReading.Kind.CHECKPOINT:
        raise ValidationError({"reading": "Choose a start or end number of this shift."})
    others = PumpMeterReading.objects.filter(shift=shift, nozzle=keep.nozzle_id, kind=keep.kind).exclude(pk=keep.pk)
    counted = _first([keep, *others])  # the number that counted until now (the first entered)
    dropped = [str(r.reading) for r in others]
    others.update(deleted_at=timezone.now())
    keep.flags = [f for f in keep.flags if f != "readings_differ"]
    keep.save(update_fields=["flags", "updated_at"])
    shift.flags = [f for f in shift.flags if f != f"readings_differ:{keep.nozzle_id}:{keep.kind}"]
    shift.save(update_fields=["flags", "updated_at"])
    audit.log("readings_resolved", actor=user, obj=keep, changes={"kept": str(keep.reading), "dropped": dropped}, request=request)
    if shift.status != Shift.Status.OPEN:
        compute_transactions(shift)
    if keep.kind != PumpMeterReading.Kind.START:  # a closing number changed: so does the start of whoever began from it
        carry_to_next_start(user=user, nozzle=keep.nozzle, old=counted.reading, new=keep.reading, after=keep.reading_at, request=request)
    return keep


# ---- pump assignment (managers and owners only) ----------------------------------------------------------------

def station_pumps(station):
    return list(Pump.objects.filter(station=station).prefetch_related("nozzles"))


def is_assigned_station(station):
    return station.assignment_mode == Station.AssignmentMode.ASSIGNED


def update_sales_so_far(shift):
    """Assigned pumps: the dashboard shows each stretch of pumping as soon as a handover number closes it."""
    if shift.status == Shift.Status.OPEN and is_assigned_station(shift.station):
        from .rollups import on_shift_progress  # rollups import this module, so import here

        on_shift_progress(shift)


def my_pumps(station, user):
    """The pumps a manager gave this attendant at this station."""
    return list(Pump.objects.filter(station=station, assigned_to=user).prefetch_related("nozzles").order_by("name"))


def open_shift_at(station):
    return Shift.objects.filter(station=station, status=Shift.Status.OPEN).order_by("started_at").first()


def open_pump_assignment(pump):
    """Who is working this pump right now (on an open shift), if anyone."""
    return (PumpAssignment.objects.filter(pump=pump, ended_at__isnull=True, shift__status=Shift.Status.OPEN)
            .select_related("shift__station", "user").first())


def _works_at(user, station):
    from apps.stations.models import StationAssignment

    limited = StationAssignment.objects.filter(user=user)
    return not limited.exists() or limited.filter(station=station).exists()


def _finished_part(shift, user):
    """This person already ended their part of this shift (handed in and left)."""
    row = attendant_row(shift, user)
    return bool(row and row.left_at is not None and row.cash_submitted_at is not None)


def _pump_readings(pump, readings):
    nozzles = {n.pk for n in pump.nozzles.all()}
    if any(item["nozzle"].pk not in nozzles for item in readings) or nozzles - {item["nozzle"].pk for item in readings}:
        raise ValidationError({"readings": f"To swap {pump.name} now, enter the meter number of every nozzle on it."})


def assign_pump(*, user, pump, to_user, readings=(), at=None, strict=True, request=None):
    """A manager or owner decides who works a pump (to_user=None: nobody).

    * Not in use: it is theirs from now on.
    * In use, no meter numbers: the person working it keeps it until they end their part of the shift; the new person
      takes it over when they start (a planned handover, for example the afternoon roster).
    * In use, with the meter number of every nozzle: swap now, on the same shift. Litres before the numbers count for
      the person who had it, litres after them for the new person.
    """
    if not can(user, "manage_shift"):
        raise PermissionDenied("Only a manager or the owner assigns pumps.")
    station = pump.station
    if not is_assigned_station(station):
        raise ValidationError({"detail": "Pumps are shared at this station."})
    if to_user is not None:
        _check_attendant_user(to_user, pump.tenant_id)
        if not _works_at(to_user, station):
            raise ValidationError({"user": f"{to_user.full_name} does not work at {station.name}."})
    at = at or timezone.now()
    current = open_pump_assignment(pump)
    swapped = None
    with db_tx.atomic():
        if current is not None and readings and (to_user is None or current.user_id != to_user.pk):
            _pump_readings(pump, readings)
            shift = current.shift
            if to_user is not None:
                require_cleared(to_user)  # someone who ended their part comes back for a new part once it is approved
            _record_many(shift, PumpMeterReading.Kind.CHECKPOINT, readings, at, user)
            current.ended_at = at
            current.save(update_fields=["ended_at", "updated_at"])
            if to_user is not None:
                _add_person(shift, to_user, at)
                PumpAssignment.objects.create(tenant_id=pump.tenant_id, shift=shift, pump=pump, user=to_user, started_at=at, assigned_by=user)
            else:
                shift.add_flag(f"pump_unassigned:{pump.pk}")
            shift.save()
            update_sales_so_far(shift)
            swapped = {"from": current.user.full_name, "to": to_user.full_name if to_user else None, "at": at.isoformat(),
                       "meters": {str(i["nozzle"].pk): str(i["reading"]) for i in readings}}
        elif current is None and to_user is not None and readings:
            # A free pump for someone already on shift here: they start on it now, at these numbers, so it shows on
            # their phone and is theirs to end. (Without numbers it is theirs from their next start.)
            on = ShiftAttendant.objects.filter(user=to_user, left_at__isnull=True, shift__station=station, shift__status=Shift.Status.OPEN).select_related("shift").first()
            if on is None:
                raise ValidationError({"user": f"{to_user.full_name} is not on shift at {station.name}. They start on {pump.name} when they start their shift."})
            _pump_readings(pump, readings)
            shift = on.shift
            for item in readings:
                had = PumpMeterReading.objects.filter(shift=shift, nozzle=item["nozzle"]).exists()
                _record_reading(shift, item["nozzle"], PumpMeterReading.Kind.CHECKPOINT if had else PumpMeterReading.Kind.START, item["reading"], at, user)
            PumpAssignment.objects.create(tenant_id=pump.tenant_id, shift=shift, pump=pump, user=to_user, started_at=at, assigned_by=user)
            shift.save()
            swapped = {"from": None, "to": to_user.full_name, "at": at.isoformat(), "meters": {str(i["nozzle"].pk): str(i["reading"]) for i in readings}}
        before = pump.assigned_to.full_name if pump.assigned_to_id else None
        pump.assigned_to = to_user
        pump.save(update_fields=["assigned_to", "updated_at"])
        audit.log("pump_reassigned" if swapped else "pump_assigned", actor=user, obj=pump, request=request,
                  changes={"pump": pump.name, "from": before, "to": to_user.full_name if to_user else None, **({"swap": swapped} if swapped else {})})
    return pump


def reassign_pump(*, user, shift, pump, to_user, readings, at=None, request=None):
    """Older phones send the swap against a shift: same as assign_pump."""
    if pump.station_id != shift.station_id:
        raise ValidationError({"pump": "This pump is not at this station."})
    return assign_pump(user=user, pump=pump, to_user=to_user, readings=readings, at=at, request=request)


IDLE_GRACE_MINUTES = 30  # a handover takes a few minutes: a free pump is only a problem once it has waited this long


def pump_gaps(stations, now=None):
    """Assigned pumps the manager still has to give to someone, for the top of their landing page.

    * no_attendant: nobody is planned for the pump (or the person planned has been switched off), so nobody can start
      on it;
    * idle: people are on shift at the station but nobody has been working the pump for IDLE_GRACE_MINUTES, for
      example it is planned for someone who is not on shift. Its litres would then belong to nobody.
    """
    now = now or timezone.now()
    out = []
    for station in stations:
        if not is_assigned_station(station):
            continue
        shift = open_shift_at(station)
        on_shift = {a.user_id: a.user.full_name for a in active_attendants(shift)} if shift else {}
        for pump in Pump.objects.filter(station=station).select_related("assigned_to").order_by("name"):
            planned = pump.assigned_to if pump.assigned_to_id and pump.assigned_to.is_active else None
            current = open_pump_assignment(pump)
            gap = {"station": str(station.pk), "station_name": station.name, "pump": str(pump.pk), "pump_name": pump.name}
            if planned is None:
                after = f" after {current.user.full_name} ends their part" if current else ""
                out.append({**gap, "kind": "no_attendant", "message": f"{pump.name} has no attendant{after}. Give it to someone so their sales on it are counted."})
                continue
            if current is not None or not on_shift:
                continue
            last = PumpAssignment.objects.filter(pump=pump, ended_at__isnull=False).order_by("-ended_at").values_list("ended_at", flat=True).first()
            since = max(shift.started_at, last) if last else shift.started_at
            if now - since < timedelta(minutes=IDLE_GRACE_MINUTES):
                continue
            who = (f"It is planned for {planned.full_name}, who is not on shift" if planned.pk not in on_shift
                   else f"{planned.full_name} is on shift but has not started on it")
            out.append({**gap, "kind": "idle", "since": since,
                        "message": f"Nobody is working {pump.name} since {timezone.localtime(since):%H:%M}. {who}. "
                                   f"On shift now: {', '.join(on_shift.values())}."})
    return out


# ---- starting, joining, leaving --------------------------------------------------------------------------------

def start_shift(*, user, station, readings, shift_id=None, started_at=None, attendants=None, assignments=None, strict=False):
    """Open a shift and record start meter numbers. Idempotent on shift_id (offline retries).

    The person starting is the lead if they are an attendant; a manager starting names the attendants (the first is lead).
    On a several-attendants station that already has an open shift, the starter joins that shift instead (two phones
    that both started offline end up on one shift). Returns the shift; `shift.merged_from` is set when merged.
    On assigned pumps the attendant starts their own shift on the pumps a manager gave them; `assignments` sent by
    older phones is ignored (only managers and owners assign pumps).
    """
    started_at = started_at or timezone.now()
    if shift_id:
        existing = Shift.all_objects.filter(pk=shift_id).first()
        if existing:
            if existing.station_id != station.pk or not (can(user, "manage_shift") or attendant_row(existing, user)):
                raise Conflict("Shift id already used.")
            existing.merged_from = None
            return existing
    for person in ([user] if user.role == Role.ATTENDANT else list(attendants or [])):
        require_cleared(person)
    if is_assigned_station(station):
        return _start_on_my_pumps(user=user, station=station, readings=readings, shift_id=shift_id, started_at=started_at, attendants=attendants, strict=strict)
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

    if not multiple:
        # One attendant per shift: never a second open shift on the same pumps (its litres would be counted twice).
        open_shift = Shift.objects.filter(station=station, status=Shift.Status.OPEN).order_by("started_at").first()
        if open_shift:
            person = people[0]
            on_it = list(active_attendants(open_shift))
            if any(a.user_id == person.pk for a in on_it):  # the same person again (a second phone, a retry)
                open_shift.merged_from = str(shift_id) if shift_id else ""
                return open_shift
            if on_it:
                names = ", ".join(a.user.full_name for a in on_it)
                message = f"A shift is already open here ({names}). It is ended, or handed over, before a new one starts."
                if strict:
                    raise ValidationError({"detail": message})
                raise Conflict(message)
            with db_tx.atomic():  # the previous person left: this is the handover, on the same shift
                _add_person(open_shift, person, started_at)
                _ensure_lead(open_shift)
                _record_many(open_shift, PumpMeterReading.Kind.CHECKPOINT, readings, started_at, user)
                open_shift.save()
            audit.log("shift_joined", actor=user, obj=open_shift, changes={"who": person.full_name, "handover": True})
            open_shift.merged_from = str(shift_id) if shift_id else ""
            return open_shift

    try:
        with db_tx.atomic():
            kwargs = {"pk": shift_id} if shift_id else {}
            shift = Shift.objects.create(tenant_id=user.tenant_id, station=station, started_at=started_at, **kwargs)
            for i, p in enumerate(people):
                _add_person(shift, p, started_at, lead=i == 0)
            _record_many(shift, PumpMeterReading.Kind.START, readings, shift.started_at, user)
            shift.save()
    except IntegrityError:
        # The id exists but is invisible to us (it belongs to another business): never reveal or reuse it.
        raise Conflict("Shift id already used.")
    shift.merged_from = None
    return shift


def _start_on_my_pumps(*, user, station, readings, shift_id, started_at, attendants, strict):
    """Assigned pumps: an attendant starts on the pumps a manager gave them (a manager may start for one attendant).
    The first person opens the station's shift; the next ones join it. A pump still with someone else is handed over
    at the numbers this person enters (offline phones), or refused until that person ends their part (online)."""
    if user.role == Role.ATTENDANT:
        person = user
    else:
        people = [_check_attendant_user(u, user.tenant_id) for u in (attendants or [])]
        if len(people) != 1:
            raise ValidationError({"attendants": "On assigned pumps each attendant starts on their own pumps. Choose one attendant."})
        person = people[0]
    pumps = my_pumps(station, person)
    if not pumps:
        raise ValidationError({"detail": f"No pump is assigned to {person.full_name} at {station.name}. A manager assigns the pumps."})
    with db_tx.atomic():
        shift = open_shift_at(station)
        merged = shift is not None
        if shift is None:
            try:
                with db_tx.atomic():
                    shift = Shift.objects.create(tenant_id=station.tenant_id, station=station, started_at=started_at, **({"pk": shift_id} if shift_id else {}))
                    _add_person(shift, person, started_at, lead=True)
            except IntegrityError:
                raise Conflict("Shift id already used.")
        else:
            _add_person(shift, person, started_at)  # after an approved end: a new part, on the pumps they have now
            _ensure_lead(shift)
        at = max(shift.started_at, started_at)
        given = {}
        for r in readings:
            given.setdefault(r["nozzle"].pump_id, []).append(r)
        mine = {a.pump_id: a.started_at for a in PumpAssignment.objects.filter(shift=shift, user=person, ended_at__isnull=True)}
        taking = []
        for p in pumps:
            nozzle_ids = {n.pk for n in p.nozzles.all()}
            got = [r for r in given.get(p.pk, []) if r["nozzle"].pk in nozzle_ids]
            if p.pk in mine:
                # Already working it. An earlier start that reaches the server late (the phone was offline) must not
                # put readings before this person's period on the pump: that would scramble the pump's numbers.
                taking.append((p, got if at >= mine[p.pk] else []))
                continue
            other = open_pump_assignment(p)
            if other is not None:
                if strict:
                    raise ValidationError({"detail": f"{p.name} is still with {other.user.full_name}. They end their part of the shift first, or a manager swaps it now."})
                if not got:
                    shift.add_flag(f"pump_busy:{p.pk}")
                    continue
                other.ended_at = at  # handed over at the numbers this person entered
                other.save(update_fields=["ended_at", "updated_at"])
                shift.add_flag(f"handover_before_end:{p.pk}")
            elif not got and not strict:
                shift.add_flag(f"pump_not_started:{p.pk}")
                continue
            PumpAssignment.objects.create(tenant_id=shift.tenant_id, shift=shift, pump=p, user=person, started_at=at)
            taking.append((p, got))
        allowed = {n.pk for p, _ in taking for n in p.nozzles.all()}
        if any(r["nozzle"].pk not in allowed for r in readings):
            if strict:
                raise ValidationError({"readings": "Enter the meters of your own pumps only."})
            shift.add_flag("reading_not_your_pump")
        if strict and allowed - {r["nozzle"].pk for _, got in taking for r in got}:
            raise ValidationError({"readings": "Enter the meter number of every nozzle on each of your pumps."})
        for _, got in taking:
            for r in got:
                last = PumpMeterReading.objects.filter(shift=shift, nozzle=r["nozzle"]).order_by("-reading_at", "-created_at").first()
                if last is None:
                    _record_reading(shift, r["nozzle"], PumpMeterReading.Kind.START, r["reading"], at, user, r.get("photo"))
                elif last.reading != Decimal(str(r["reading"])).quantize(TWO) or last.reading_at != at:
                    # taking a pump over inside the shift: this person's period starts at this number
                    _record_reading(shift, r["nozzle"], PumpMeterReading.Kind.CHECKPOINT, r["reading"], at, user, r.get("photo"))
        shift.save()
        update_sales_so_far(shift)
    shift.merged_from = (str(shift_id) if shift_id else "") if merged else None
    return shift


def _end_my_part(*, user, shift, readings, cash, ended_at, strict, who, request, cash_later=False):
    """Assigned pumps: one attendant ends their part of the shift. The meter numbers of their pumps close their
    periods (and pass the pumps on), their cash is theirs, and they leave the shift. The last person closes it.
    cash_later: only the meters now (the shift change goes on); the cash follows once it is counted."""
    person = who or (user if attendant_row(shift, user) else None)
    if person is None:
        raise ValidationError({"user": "Choose whose part of the shift ends."})
    if person.pk != user.pk and not can(user, "manage_shift"):
        raise PermissionDenied("You can only end your own part of the shift.")
    if _finished_part(shift, person):  # a resend: only the cash can still be corrected
        submit_cash(user=user, shift=shift, cash=cash, who=person, at=ended_at, request=request)
        return
    mine = attendant_row(shift, person)
    if mine and mine.approval == CASH_DUE:  # meters already closed: this brings the cash (or repeats the meters)
        if cash:
            submit_cash(user=user, shift=shift, cash=cash, who=person, at=ended_at, request=request, part=mine)
        return
    holding = list(PumpAssignment.objects.filter(shift=shift, user=person, ended_at__isnull=True).select_related("pump__assigned_to"))
    held = {a.pump_id for a in holding}
    keep = []
    for r in readings:
        if r["nozzle"].pump_id in held:
            keep.append(r)
        elif strict:
            raise ValidationError({"readings": f"{r['nozzle'].pump.name} is not with you any more."})
        else:
            shift.add_flag(f"reading_after_swap:{r['nozzle'].pk}")
    if strict:
        needed = set(Nozzle.objects.filter(pump_id__in=held).values_list("pk", flat=True))
        if needed - {r["nozzle"].pk for r in keep}:
            raise ValidationError({"readings": "Enter the meter number of every nozzle on your pumps."})
    last = not [a for a in active_attendants(shift) if a.user_id != person.pk and not a.done]
    _record_many(shift, PumpMeterReading.Kind.END if last else PumpMeterReading.Kind.CHECKPOINT, keep, ended_at, user)
    passed = []
    for a in holding:
        a.ended_at = ended_at
        a.save(update_fields=["ended_at", "updated_at"])
        # The pump goes to the person a manager planned it for. Already on this shift: their period starts now, at the
        # numbers just taken, so their phone shows it straight away. Not here yet: they take it when they start.
        incoming = a.pump.assigned_to
        if incoming is not None and incoming.pk != person.pk and is_on_shift(incoming, shift):
            PumpAssignment.objects.create(tenant_id=shift.tenant_id, shift=shift, pump=a.pump, user=incoming, started_at=ended_at)
            passed.append(f"{a.pump.name} to {incoming.full_name}")
    shift.save()
    if not cash_later:
        submit_cash(user=user, shift=shift, cash=cash, who=person, at=ended_at, request=request)
    row = attendant_row(shift, person)
    if row.left_at is None:
        row.left_at = ended_at
        if cash_later:
            row.approval = CASH_DUE
        row.save(update_fields=["left_at", "approval", "updated_at"])
        _ensure_lead(shift)
    audit.log("shift_part_ended", actor=user, obj=shift, request=request,
              changes={"who": person.full_name, "pumps": [a.pump.name for a in holding], "last": last, **({"passed_on": passed} if passed else {}),
                       **({"cash": "to hand in after counting"} if cash_later else {})})
    if last:
        shift.close_requested_at = shift.close_requested_at or ended_at
        shift.closed_by = user
        shift.save(update_fields=["close_requested_at", "closed_by", "updated_at"])
        maybe_close(shift)
    update_sales_so_far(shift)  # still open: the sales of the part that just ended show now


def join_shift(*, user, shift, who=None, at=None, request=None):
    """Someone arrives mid-shift. On a one-attendant station this is a handover: the current person must leave first."""
    who = who or user
    if who.pk != user.pk:
        require_manage(user, shift)
    _require_open(shift)
    if is_assigned_station(shift.station):
        raise ValidationError({"detail": "On assigned pumps, start your shift on your own pumps: you join the open shift."})
    _check_attendant_user(who, shift.tenant_id)
    require_cleared(who)
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

def submit_cash(*, user, shift, cash, who=None, at=None, request=None, part=None):
    """One person's handover by payment type. Re-sending before review corrects it (the audit log keeps the old amounts)."""
    who = who or user
    if who.pk != user.pk:
        require_manage(user, shift)
    _require_not_reviewed(shift)
    row = part or attendant_row(shift, who)  # their current part, unless an earlier part is being fixed
    if row is None:
        raise ValidationError({"detail": "This person is not on the shift."})
    handed_in = row.cash_submitted_at is not None
    current = {(c.payment_type, c.currency): c.amount for c in CashSubmission.objects.filter(part=row)}
    lines = [(c["payment_type"], c["currency"], Decimal(str(c["amount"])).quantize(TWO)) for c in cash]
    differs = any(current.get((t, cur)) != amount for t, cur, amount in lines if not (amount == 0 and (t, cur) not in current))
    grant = unlock_change(user, row, UndoRequest.Kind.CASH) if handed_in and differs else None  # a same-numbers resend is fine
    changes = {}
    for payment_type, currency, amount in lines:
        obj, created = CashSubmission.objects.get_or_create(
            part=row, payment_type=payment_type, currency=currency,
            defaults={"tenant_id": shift.tenant_id, "shift": shift, "attendant": who, "amount": amount},
        )
        if created and handed_in:
            changes[f"{payment_type}:{currency}"] = [None, str(amount)]
        elif not created and obj.amount != amount:
            changes[f"{payment_type}:{currency}"] = [str(obj.amount), str(amount)]
            obj.amount = amount
            obj.save(update_fields=["amount", "updated_at"])
    if not handed_in:
        _mark_handed_in(row, user, at or timezone.now())
    elif differs:
        after_change(user, row, grant)
    if changes:
        audit.log("cash_corrected", actor=user, obj=shift, changes={"who": who.full_name, **changes}, request=request)
    maybe_close(shift)
    if not handed_in and shift.status != Shift.Status.OPEN:  # cash counted after the shift closed: its cash check is rebuilt
        from .rollups import on_shift_closed  # rollups import this module, so import here

        on_shift_closed(shift)
    return row


def pending_people(shift):
    """People still on the shift who have not handed in (they must hand in or be marked as left before it closes)."""
    return [a for a in active_attendants(shift) if not a.done]


def missing_end_nozzles(shift):
    started = set(PumpMeterReading.objects.filter(shift=shift, kind=PumpMeterReading.Kind.START).values_list("nozzle_id", flat=True))
    ended = set(PumpMeterReading.objects.filter(shift=shift, kind=PumpMeterReading.Kind.END).values_list("nozzle_id", flat=True))
    if PumpAssignment.objects.filter(shift=shift).exists():  # assigned pumps: a handover number also closes a nozzle
        ended |= set(PumpMeterReading.objects.filter(shift=shift, kind=PumpMeterReading.Kind.CHECKPOINT).values_list("nozzle_id", flat=True))
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


def end_shift(*, user, shift, readings, cash, ended_at=None, strict=False, close=None, who=None, request=None, cash_later=False):
    """'End of my shift' from a phone: end meter numbers (anyone on the shift), the sender's cash, and, for the lead or a
    manager, a request to close. Re-sending corrects an unreviewed shift. Assigned pumps: cash_later closes the meters now
    (the pumps pass on at once) and the cash is handed in after it is counted."""
    _require_not_reviewed(shift)
    ended_at = ended_at or timezone.now()
    due = attendant_row(shift, who or user)
    if is_assigned_station(shift.station) and (shift.status == Shift.Status.OPEN or (due and due.approval == CASH_DUE)):
        if not (can(user, "manage_shift") or attendant_row(shift, user)):
            raise PermissionDenied("This is not your shift.")
        with db_tx.atomic():
            _end_my_part(user=user, shift=shift, readings=readings, cash=cash, ended_at=ended_at, strict=strict, who=who, request=request,
                         cash_later=cash_later)
        shift.refresh_from_db()
        return shift
    if cash_later:
        raise ValidationError({"detail": "Closing the meters first and handing in the cash after is for assigned pumps."})
    require_on_shift(user, shift)
    was_closed = shift.status != Shift.Status.OPEN
    end_readings = _readings_for_end(shift, readings, strict)
    # Someone who already handed in sending different end numbers: that is a change, checked like a cash change.
    sender = attendant_row(shift, user)
    meters_changed = bool(sender and sender.cash_submitted_at and _new_end_numbers(shift, end_readings))
    grant = unlock_change(user, sender, UndoRequest.Kind.METERS) if meters_changed else None
    with db_tx.atomic():
        _record_many(shift, PumpMeterReading.Kind.END, end_readings, ended_at, user)
        shift.save()
        lead = lead_of(shift)
        person = who or (user if attendant_row(shift, user) else (lead.user if lead else None))
        if person is not None and (cash or person.pk == user.pk):
            submit_cash(user=user, shift=shift, cash=cash, who=person, at=ended_at, request=request)
        if meters_changed:
            sender.refresh_from_db()
            after_change(user, sender, grant)
        if was_closed:  # a correction before review: rebuild the numbers
            compute_transactions(shift)
            from .rollups import on_shift_closed

            on_shift_closed(shift)
        elif close if close is not None else can_manage(user, shift):
            request_close(user=user, shift=shift, at=ended_at, strict=strict, request=request)
    shift.refresh_from_db()
    return shift


def _readings_for_end(shift, readings, strict):
    """Assigned pumps: end numbers only for pumps still on this shift. A pump a manager swapped away already has its
    end number (taken at the swap); a phone that sends one later (it was offline) is ignored and flagged."""
    rows = list(PumpAssignment.objects.filter(shift=shift))
    if not rows:
        return readings
    holding = {a.pump_id for a in rows if a.ended_at is None}
    had = {a.pump_id for a in rows}
    keep = []
    for r in readings:
        pump_id = r["nozzle"].pump_id
        if pump_id in holding or (shift.status != Shift.Status.OPEN and pump_id in had):
            keep.append(r)
        elif strict:
            raise ValidationError({"readings": f"{r['nozzle'].pump.name} is not on your shift any more."})
        else:
            shift.add_flag(f"reading_after_swap:{r['nozzle'].pk}")
    return keep


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
    for r in PumpMeterReading.objects.filter(shift=shift).select_related("nozzle__pump", "nozzle__tank__fuel_type"):
        by_nozzle.setdefault(r.nozzle_id, {"start": [], "checkpoint": [], "end": []})[r.kind].append(r)
    shift.flags = [f for f in shift.flags if not f.startswith(("no_price", "missing_end", "meter_", "unassigned_period"))]
    created = []
    for rows in by_nozzle.values():
        if not rows["start"]:
            continue
        checkpoints, seen = [], set()
        for r in sorted(rows["checkpoint"], key=lambda r: (r.reading_at, r.created_at)):
            if r.reading_at not in seen:  # two people snapshotting the same moment count once
                seen.add(r.reading_at)
                checkpoints.append(r)
        # in time order: a reading that reached the server late (an offline phone) still takes its own place
        points = sorted([_first(rows["start"]), *checkpoints], key=lambda r: (r.reading_at, r.created_at))
        if rows["end"]:
            chain = [*points, _first(rows["end"])]
        elif assigned and len(points) > 1:
            chain = points  # assigned pumps: the nozzle's last handover number closes it for this shift
        else:
            if shift.status != Shift.Status.OPEN:  # on an open shift the pump is simply still in use
                shift.add_flag(f"missing_end:{rows['start'][0].nozzle_id}")
            continue
        nozzle = chain[0].nozzle
        fuel_type = nozzle.tank.fuel_type
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


# ---- what the phone should show -------------------------------------------------------------------------------

def phone_stage(user, station):
    """Where this person is in the shift cycle at this station, so any phone (after signing out and in, or a new
    phone) shows the right screen: {"stage": "start"} | {"stage": "on_shift", "shift": {...}}.

    On shift: the shift id, when it started, whether they only hand in cash (joined someone else's shared-pump shift),
    and the meter numbers their part started from, per nozzle (so the end screen can show and check them).

    {"stage": "held", ...}: their last end of shift waits for a manager or was sent back to fix (no new shift until it
    is approved), or the owner allowed them to change something they handed in (see approvals.end_stage)."""
    if user.role == Role.ATTENDANT:
        from .approvals import end_stage, end_summary, fix_grant  # approvals builds on this module

        if (due := cash_due_part(user)) is not None and due.shift.station_id == station.pk:
            # meters closed at the shift change: count the cash against what the meters say and hand it in
            return {"stage": "cash_due", "shift_id": str(due.shift_id), "end_id": str(due.pk), "ended_at": due.left_at, **end_summary(due)}
        if (held := held_end(user)) is not None:
            return end_stage(held)
        stage = _shift_stage(user, station)
        if stage["stage"] != "on_shift" and (grant := fix_grant(user)) is not None:
            return end_stage(grant.attendant)
        return stage
    return _shift_stage(user, station)


def _shift_stage(user, station):
    shift = (Shift.objects.filter(station=station, status=Shift.Status.OPEN, attendants__user=user).order_by("started_at").first())
    if shift is None:
        return {"stage": "start"}
    row = attendant_row(shift, user)
    if row.left_at is not None:
        # left, or ended their part (approved, else phone_stage shows it as held): they may start a new part
        return {"stage": "start"}
    readings = list(PumpMeterReading.objects.filter(shift=shift).exclude(kind=PumpMeterReading.Kind.END).order_by("reading_at", "created_at"))
    starts = {}
    if is_assigned_station(station):
        for a in PumpAssignment.objects.filter(shift=shift, user=user, ended_at__isnull=True).prefetch_related("pump__nozzles"):
            for n in a.pump.nozzles.all():
                mine = [r for r in readings if r.nozzle_id == n.pk and r.reading_at >= a.started_at]
                if mine:
                    starts[str(n.pk)] = str(mine[0].reading)
        member = False
    else:
        for r in readings:
            if r.kind == PumpMeterReading.Kind.START and str(r.nozzle_id) not in starts:
                starts[str(r.nozzle_id)] = str(r.reading)
        member = station.attendants_mode == Station.AttendantsMode.MULTIPLE and not row.is_lead
    return {"stage": "on_shift", "shift": {"id": str(shift.pk), "started_at": shift.started_at.isoformat(), "member": member, "starts": starts}}


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
