"""End-of-shift approval and owner-allowed undo.

* An attendant ends their part (meters and cash) as before; their pumps pass on at once. Their end then waits for a
  manager of the station (or the owner): Approve, or Send back with a reason. Until it is approved they cannot start or
  join another shift (services.require_cleared).
* Sent back: the attendant fixes it themselves (cash and their end meter numbers) and it goes back to the manager.
* Anything else already handed in changes only with the owner's permission: the attendant asks to undo (change cash,
  change meter numbers, or "I ended by mistake"), the owner allows or refuses. An allowed change can be made once.
  "Ended by mistake" puts them back on the shift as soon as the owner allows it.
Managers and the owner correct directly. Every step is in the audit log.
"""
from decimal import Decimal

from django.db import transaction as db_tx
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.accounts.permissions import can, require
from apps.audit import services as audit
from apps.stations.access import scope
from apps.stations.services import price_at, rate_at

from . import services
from .models import CashSubmission, PumpAssignment, PumpMeterReading, Shift, ShiftAttendant, Transaction, UndoRequest
from .rollups import on_shift_closed

APPROVAL = ShiftAttendant.Approval
KIND = UndoRequest.Kind
STATUS = UndoRequest.Status
TWO, THREE = Decimal("0.01"), Decimal("0.001")
END_KINDS = (PumpMeterReading.Kind.END, PumpMeterReading.Kind.CHECKPOINT)


# ---- what an end of shift contains -------------------------------------------------------------------------------

def _end_points(row):
    """{nozzle id: (nozzle, start reading, [end reading rows], end kind, end time)} for the meters this person ended on.
    Assigned pumps: each of their pump periods. Shared pumps: the shift's numbers, if they were its lead."""
    shift = row.shift
    readings = list(PumpMeterReading.objects.filter(shift=shift).select_related("nozzle__pump", "nozzle__tank__fuel_type").order_by("reading_at", "created_at"))
    out = {}
    periods = list(_periods(row))
    if periods:
        for a in periods:
            for n in a.pump.nozzles.all():
                before = [r for r in readings if r.nozzle_id == n.pk and r.reading_at <= a.started_at]
                ends = [r for r in readings if a.ended_at and r.nozzle_id == n.pk and r.reading_at == a.ended_at and r.kind in END_KINDS]
                out[n.pk] = (n, before[-1] if before else None, ends, ends[0].kind if ends else None, a.ended_at)
        return out
    if not row.is_lead:
        return out  # a shared-pump helper hands in cash only; the lead enters the meters
    for r in readings:
        if r.kind == PumpMeterReading.Kind.START and r.nozzle_id not in out:
            ends = [e for e in readings if e.nozzle_id == r.nozzle_id and e.kind == PumpMeterReading.Kind.END]
            out[r.nozzle_id] = (r.nozzle, r, ends, PumpMeterReading.Kind.END, ends[0].reading_at if ends else None)
    return out


def _periods(row):
    """This part's pump periods: those that began while it was open (a person may work two parts of one shift)."""
    qs = PumpAssignment.objects.filter(shift=row.shift, user=row.user, started_at__gte=row.joined_at).select_related("pump").order_by("started_at")
    return qs.filter(started_at__lte=row.left_at) if row.left_at else qs


def _cash(row):
    """This part's cash (rows handed in before cash was kept per part belong to the person's only part)."""
    return CashSubmission.objects.filter(Q(part=row) | Q(part__isnull=True, shift=row.shift, attendant=row.user)).order_by("payment_type")


def end_summary(row):
    """Meters (start, end, litres, amount at the price in force), cash by payment type, and the cash difference when it
    can be worked out for this one person (assigned pumps, or alone on the shift)."""
    shift, station = row.shift, row.shift.station
    currency = station.pricing_currency
    meters, expected, known = [], Decimal(0), True
    for n, start, ends, _, _ in _end_points(row).values():
        end = ends[0] if ends else None
        line = {"nozzle": str(n.pk), "pump": n.pump.name, "label": str(n), "fuel_code": n.tank.fuel_type.code,
                "start": str(start.reading) if start else None, "end": str(end.reading) if end else None, "litres": None, "amount": None}
        if start and end:
            litres = services.dispensed_litres(start.reading, end.reading, n.meter_max, [])
            price = price_at(station.pk, n.tank.fuel_type_id, currency, start.reading_at)
            line["litres"] = str(litres.quantize(THREE))
            if price:
                amount = (litres * price.price_per_litre).quantize(TWO)
                line["amount"] = str(amount)
                expected += amount
            else:
                known = False
        else:
            known = False
        meters.append(line)
    cash, handed = [], Decimal(0)
    at = row.cash_submitted_at or timezone.now()
    for c in _cash(row):
        cash.append({"payment_type": c.payment_type, "currency": c.currency, "amount": str(c.amount)})
        rate = Decimal(1) if c.currency == currency else rate_at(c.currency, currency, at)
        if rate is None:
            known = False
        else:
            handed += (c.amount * rate).quantize(TWO)
    alone = shift.attendants.values("user").distinct().count() == 1  # one person, maybe in two parts
    mine = bool(meters) and (_periods(row).exists() or alone)
    return {
        "person": row.user.full_name, "currency": currency, "meters": meters, "cash": cash,
        "handed_in": str(handed.quantize(TWO)),
        "expected": str(expected.quantize(TWO)) if (mine and known) else None,
        "cash_difference": str((handed - expected).quantize(TWO)) if (mine and known) else None,
    }


def _undo_json(req):
    return {"id": str(req.pk), "kind": req.kind, "kind_label": req.get_kind_display(), "status": req.status, "reason": req.reason,
            "decision_note": req.decision_note, "created_at": req.created_at, "decided_at": req.decided_at,
            "decided_by": req.decided_by.full_name if req.decided_by_id else None}


def end_card(row):
    """One end of shift, as the manager and the attendant see it."""
    last = UndoRequest.objects.filter(attendant=row).select_related("decided_by").order_by("-created_at").first()
    return {"id": str(row.pk), "shift_id": str(row.shift_id), "station": row.shift.station.name, "station_id": str(row.shift.station_id),
            "status": row.approval or APPROVAL.APPROVED, "ended_at": row.cash_submitted_at, "shift_started_at": row.shift.started_at,
            "note": row.approval_note, "approved_by": row.approval_by.full_name if row.approval_by_id else None, "approved_at": row.approval_at,
            "undo": _undo_json(last) if last else None, **end_summary(row)}


def end_stage(row):
    """The attendant's phone: their end waits for the manager, was sent back, or may be changed (owner allowed it)."""
    card = end_card(row)
    allowed = set(UndoRequest.objects.filter(attendant=row, status=STATUS.ALLOWED).values_list("kind", flat=True))
    sent_back = row.approval == APPROVAL.SENT_BACK
    has_meters = any(m["end"] is not None for m in card["meters"])
    return {"stage": "held", "end_id": card["id"], **{k: v for k, v in card.items() if k != "id"},
            "can_fix": {"cash": sent_back or KIND.CASH in allowed, "meters": has_meters and (sent_back or KIND.METERS in allowed)}}


def fix_grant(user):
    """A change the owner allowed this attendant and they have not made yet."""
    return (UndoRequest.objects.filter(attendant__user=user, status=STATUS.ALLOWED, kind__in=[KIND.CASH, KIND.METERS])
            .select_related("attendant__shift__station", "attendant__user").order_by("-created_at").first())


def ends_for(user, status=None):
    """Ends waiting for a manager (and those sent back) at the stations this manager runs; the owner sees all."""
    qs = scope(ShiftAttendant.objects.select_related("shift__station", "user", "approval_by"), user, "shift__station_id")
    return qs.filter(approval__in=[status] if status else list(services.HELD)).order_by("cash_submitted_at")


def get_end(user, pk):
    """An end of shift this user may see: their own, or (managers, owner) one at their stations."""
    try:
        row = ShiftAttendant.objects.select_related("shift__station", "user").filter(pk=pk).first()
    except Exception:  # noqa: BLE001 - a malformed id is simply not found
        row = None
    if row is None or not (row.user_id == user.pk or (can(user, "approve_shift_end") and scope(Shift.objects.filter(pk=row.shift_id), user).exists())):
        raise NotFound("Shift end not found.")
    return row


# ---- the manager --------------------------------------------------------------------------------------------------

def approve(*, user, row, request=None):
    require(user, "approve_shift_end", "Only a manager or the owner approves the end of a shift.")
    if row.approval != APPROVAL.PENDING:
        raise ValidationError({"detail": "This end of shift is not waiting for approval."})
    row.approval, row.approval_by, row.approval_at = APPROVAL.APPROVED, user, timezone.now()
    row.save(update_fields=["approval", "approval_by", "approval_at", "updated_at"])
    audit.log("shift_end_approved", actor=user, obj=row.shift, request=request, changes={"who": row.user.full_name})
    return row


def send_back(*, user, row, note, request=None):
    require(user, "approve_shift_end", "Only a manager or the owner sends an end of shift back.")
    note = (note or "").strip()
    if not note:
        raise ValidationError({"note": "Say what needs fixing, for example: recount the cash."})
    if row.approval != APPROVAL.PENDING:
        raise ValidationError({"detail": "This end of shift is not waiting for approval."})
    if row.shift.status == Shift.Status.REVIEWED:
        raise ValidationError({"detail": "This shift is already reviewed."})
    row.approval, row.approval_by, row.approval_at, row.approval_note = APPROVAL.SENT_BACK, user, timezone.now(), note
    row.save(update_fields=["approval", "approval_by", "approval_at", "approval_note", "updated_at"])
    audit.log("shift_end_sent_back", actor=user, obj=row.shift, request=request, changes={"who": row.user.full_name, "note": note})
    return row


# ---- the attendant fixes (sent back, or owner allowed) ------------------------------------------------------------

def fix_end(*, user, row, cash=(), readings=(), request=None):
    """Change a handed-in end: cash by payment type and/or this person's end meter numbers. Allowed when the manager
    sent it back, the owner allowed that kind of undo, or a manager/owner does it. It then goes back to the manager."""
    if user.pk != row.user_id and not can(user, "manage_shift"):
        raise PermissionDenied("You can only fix your own end of shift.")
    shift = row.shift
    if shift.status == Shift.Status.REVIEWED:
        raise ValidationError({"detail": "This shift is already reviewed. Ask a manager to correct it."})
    if row.cash_submitted_at is None:
        raise ValidationError({"detail": "This shift has not been ended yet: change the numbers on the end screen."})
    points = _end_points(row)
    changes = []
    for item in readings:
        point = points.get(item["nozzle"].pk)
        if point is None or not point[2]:
            raise ValidationError({"readings": f"{item['nozzle'].pump.name} is not one of the pumps you ended on."})
        value = Decimal(str(item["reading"])).quantize(TWO)
        if value != point[2][0].reading:
            changes.append((point, value))
    if not cash and not changes and row.approval != APPROVAL.SENT_BACK:
        raise ValidationError({"detail": "Nothing changed."})
    meter_grant = services.unlock_change(user, row, KIND.METERS) if changes else None  # checked before cash moves it on
    with db_tx.atomic():
        if cash:
            services.submit_cash(user=user, shift=shift, cash=cash, who=row.user, at=row.cash_submitted_at, request=request, part=row)
        now = timezone.now()
        for (nozzle, _, ends, kind, at), value in changes:
            PumpMeterReading.objects.filter(pk__in=[e.pk for e in ends]).update(deleted_at=now)
            PumpMeterReading.objects.create(tenant_id=shift.tenant_id, shift=shift, nozzle=nozzle, kind=kind, reading=value, reading_at=at,
                                            source="manual", recorded_by=user, corrected=True)
            if kind == PumpMeterReading.Kind.END:
                services._mark_differences(shift, nozzle, kind)
        if changes:
            row.refresh_from_db()
            services.after_change(user, row, meter_grant)
            shift.save()
        row.refresh_from_db()
        if row.approval == APPROVAL.SENT_BACK and not can(user, "manage_shift"):
            # sent back, checked and sent again unchanged ("it was right"): it still goes back to the manager
            services.after_change(user, row, None)
        if shift.status != Shift.Status.OPEN:
            services.compute_transactions(shift)
            on_shift_closed(shift)
        elif changes:
            services.update_sales_so_far(shift)
        audit.log("shift_end_fixed", actor=user, obj=shift, request=request, changes={
            "who": row.user.full_name, "meters": {str(p[0].pk): [str(p[2][0].reading), str(v)] for p, v in changes}})
    row.refresh_from_db()
    return row


# ---- undo: the owner decides --------------------------------------------------------------------------------------

def request_undo(*, user, row, kind, reason, request=None):
    if user.pk != row.user_id and not can(user, "manage_shift"):
        raise PermissionDenied("You can only ask about your own shift.")
    if kind not in KIND.values:
        raise ValidationError({"kind": "Choose what to undo."})
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Say why, so the owner can decide."})
    if row.cash_submitted_at is None:
        raise ValidationError({"detail": "Nothing is handed in yet: you can still change it before you end."})
    if row.shift.status == Shift.Status.REVIEWED:
        raise ValidationError({"detail": "This shift is already reviewed. Ask a manager to correct it."})
    if row.approval == APPROVAL.SENT_BACK and kind != KIND.REOPEN:
        raise ValidationError({"detail": "Your manager sent it back: you can fix it now without asking."})
    existing = UndoRequest.objects.filter(attendant=row, kind=kind, status__in=[STATUS.PENDING, STATUS.ALLOWED]).first()
    if existing:
        return existing
    req = UndoRequest.objects.create(tenant_id=row.tenant_id, shift=row.shift, attendant=row, kind=kind, reason=reason, requested_by=user)
    audit.log("undo_requested", actor=user, obj=row.shift, request=request, changes={"who": row.user.full_name, "kind": kind, "reason": reason})
    _tell_owner(req)
    return req


def _tell_owner(req):
    """A message to the owner's summary phone (fails soft: the request is saved either way, rule 10)."""
    from apps.insights.messaging import send_message
    from apps.tenants.services import get_settings

    cfg = get_settings(req.tenant_id)
    row = req.attendant
    send_message(tenant_id=req.tenant_id, channel=cfg.summary_channel, to=cfg.summary_phone, kind="undo_request",
                 body=f"{row.user.full_name} ({row.shift.station.name}) asks: {req.get_kind_display()}. Reason: {req.reason} "
                      "Allow or refuse in PumpWise, Approvals.")


def decide_undo(*, user, req, allow, note="", request=None):
    require(user, "allow_undo", "Only the owner allows an undo.")
    if req.status != STATUS.PENDING:
        raise ValidationError({"detail": "This request has already been answered."})
    now = timezone.now()
    with db_tx.atomic():
        req.status = STATUS.ALLOWED if allow else STATUS.REFUSED
        req.decided_by, req.decided_at, req.decision_note = user, now, (note or "").strip()
        req.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
        if allow and req.kind == KIND.REOPEN:
            reopen_part(user=user, row=req.attendant)
            req.status, req.used_at = STATUS.USED, now
            req.save(update_fields=["status", "used_at", "updated_at"])
        audit.log("undo_allowed" if allow else "undo_refused", actor=user, obj=req.shift, request=request,
                  changes={"who": req.attendant.user.full_name, "kind": req.kind, "note": req.decision_note})
    return req


def reopen_part(*, user, row):
    """"I ended my shift by mistake": take back their end so they are on the shift again, as if they never ended.
    Their end meter numbers and cash are set aside (soft-deleted); a closed shift opens again and its sales come off
    the reports until it closes. Refused when someone else has since taken their pump or a new shift has started."""
    shift, station = row.shift, row.shift.station
    if shift.status == Shift.Status.REVIEWED:
        raise ValidationError({"detail": "This shift is already reviewed. Ask a manager to correct it."})
    if ShiftAttendant.objects.filter(shift=shift, user=row.user, joined_at__gt=row.joined_at).exists():
        raise ValidationError({"detail": "They have started again on this shift since. Ask the manager to correct the numbers instead."})
    periods = list(PumpAssignment.objects.filter(shift=shift, user=row.user, ended_at=row.left_at).select_related("pump")) if row.left_at else []
    for a in periods:
        later = PumpAssignment.objects.filter(shift=shift, pump=a.pump, started_at__gte=a.ended_at).exclude(pk=a.pk).select_related("user").first()
        busy = services.open_pump_assignment(a.pump)
        taken = later or (busy if busy and busy.shift_id != shift.pk else None)
        if taken:
            raise ValidationError({"detail": f"{a.pump.name} is now with {taken.user.full_name}. Ask the manager to correct the numbers instead."})
    if shift.status != Shift.Status.OPEN and Shift.objects.filter(station=station, status=Shift.Status.OPEN).exists():
        raise ValidationError({"detail": f"A new shift has started at {station.name}. Ask the manager to correct the numbers instead."})
    now = timezone.now()
    with db_tx.atomic():
        for a in periods:
            PumpMeterReading.objects.filter(shift=shift, nozzle__pump=a.pump, reading_at=a.ended_at, kind__in=END_KINDS).update(deleted_at=now)
            a.ended_at = None
            a.save(update_fields=["ended_at", "updated_at"])
        if not periods and shift.status != Shift.Status.OPEN:
            PumpMeterReading.objects.filter(shift=shift, kind=PumpMeterReading.Kind.END).update(deleted_at=now)
        _cash(row).update(deleted_at=now)
        row.left_at = row.cash_submitted_at = row.approval_by = row.approval_at = None
        row.approval = APPROVAL.NONE
        row.save(update_fields=["left_at", "cash_submitted_at", "approval", "approval_by", "approval_at", "updated_at"])
        if shift.status != Shift.Status.OPEN:
            shift.status, shift.ended_at, shift.close_requested_at, shift.closed_by = Shift.Status.OPEN, None, None, None
            shift.save()
            Transaction.objects.filter(shift=shift).update(deleted_at=now)
            on_shift_closed(shift)  # the day's sales and the shift summary without it, until it closes again
        services._ensure_lead(shift)
        audit.log("shift_end_reopened", actor=user, obj=shift, changes={"who": row.user.full_name})
    return row


def undo_requests_for(user, status=STATUS.PENDING):
    require(user, "allow_undo", "Only the owner sees undo requests.")
    qs = UndoRequest.objects.select_related("attendant__user", "shift__station", "decided_by", "requested_by")
    return qs.filter(status=status) if status else qs


def undo_card(req):
    return {**_undo_json(req), "end_id": str(req.attendant_id), "person": req.attendant.user.full_name, "station": req.shift.station.name,
            "requested_by": req.requested_by.full_name, "shift_started_at": req.shift.started_at, "end": end_summary(req.attendant)}
