"""What people do with alerts, and what the system does when nobody does anything.

* A manager (their stations) or the owner resolves an open alert by picking HOW it was resolved, plus a note.
* Write-offs (real fuel loss, an attendant paying back a cash difference) resolved by a manager wait for the owner's
  approval; the owner approves or sends them back.
* Everything a manager resolves lands in the owner's "To review" list until the owner accepts it or sends it back.
* Open 12 hours: the station's manager is reminded. Open 24 hours: the owner is told.
* Every step is an AlertEvent, so the owner can read each alert's story: who did what, when, and why.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import Role, User
from apps.accounts.permissions import can

from .models import Alert, AlertEvent

OPEN, AWAITING, RESOLVED, AUTO = "open", "awaiting_approval", "resolved", "auto_resolved"
REMIND_AFTER = timedelta(hours=12)
ESCALATE_AFTER = timedelta(hours=24)
REOPEN_WITHIN = timedelta(hours=24)

# (code, what the person picks, write-off: needs the owner's approval when a manager picks it)
REASONS = {
    "loss": [
        ("dip_misread", "Dip was misread: re-dipped", False),
        ("delivery_missing", "A delivery was not recorded: now added", False),
        ("meter_typo", "A meter number was mistyped: corrected", False),
        ("real_loss", "Real loss: fuel is missing", True),
        ("other", "Other (explain in the note)", False),
    ],
    "cash": [
        ("typing_error", "Typing error: corrected", False),
        ("handed_in_late", "Money was handed in late", False),
        ("attendant_pays", "Attendant will pay back the difference", True),
        ("other", "Other (explain in the note)", False),
    ],
    "low_stock": [
        ("delivery_ordered", "Fuel ordered", False),
        ("delivery_received", "Delivery received and recorded", False),
        ("other", "Other (explain in the note)", False),
    ],
    "missed_reading": [
        ("dip_done", "Dip done", False),
        ("shift_closed", "Shift closed", False),
        ("other", "Other (explain in the note)", False),
    ],
}


def reason_label(kind, code):
    return next((label for c, label, _ in REASONS.get(kind, []) if c == code), code)


def is_write_off(kind, code):
    return any(c == code and w for c, _, w in REASONS.get(kind, []))


def log_event(alert, kind, actor=None, note="", **data):
    return AlertEvent.objects.create(tenant_id=alert.tenant_id, alert=alert, kind=kind, actor=actor, note=note or "", data=data)


def _require_owner(user):
    if not can(user, "owner"):
        raise PermissionDenied("Only the owner can do this.")


@transaction.atomic
def resolve(*, user, alert, reason, note=""):
    if alert.status != OPEN:
        raise ValidationError({"detail": "This alert is not open."})
    codes = [c for c, _, _ in REASONS.get(alert.kind, [])]
    if reason not in codes:
        raise ValidationError({"reason": "Choose how it was resolved."})
    note = (note or "").strip()
    write_off = is_write_off(alert.kind, reason)
    if (reason == "other" or write_off) and not note:
        raise ValidationError({"note": "Add a note explaining what happened."})
    now = timezone.now()
    alert.resolution_reason, alert.resolution_note, alert.resolved_by = reason, note, user
    owner = can(user, "owner")
    if write_off and not owner:
        alert.status = AWAITING
        alert.save()
        log_event(alert, "submitted_for_approval", user, note, reason=reason, label=reason_label(alert.kind, reason))
        return alert
    alert.status, alert.resolved_at = RESOLVED, now
    if owner:  # the owner's own resolutions need no review
        alert.reviewed_at, alert.reviewed_by = now, user
    alert.save()
    log_event(alert, "resolved", user, note, reason=reason, label=reason_label(alert.kind, reason))
    return alert


@transaction.atomic
def approve(*, user, alert, note=""):
    _require_owner(user)
    if alert.status != AWAITING:
        raise ValidationError({"detail": "Nothing is waiting for approval on this alert."})
    now = timezone.now()
    alert.status, alert.resolved_at, alert.reviewed_at, alert.reviewed_by = RESOLVED, now, now, user
    alert.save()
    log_event(alert, "approved", user, note)
    return alert


@transaction.atomic
def accept(*, user, alert):
    """The owner has read how a manager resolved it and is happy with it."""
    _require_owner(user)
    if alert.status != RESOLVED or alert.reviewed_at is not None:
        raise ValidationError({"detail": "There is nothing to review on this alert."})
    alert.reviewed_at, alert.reviewed_by = timezone.now(), user
    alert.save(update_fields=["reviewed_at", "reviewed_by", "updated_at"])
    log_event(alert, "reviewed", user)
    return alert


@transaction.atomic
def send_back(*, user, alert, note):
    """The owner is not satisfied: the alert opens again for the manager, with the owner's note."""
    _require_owner(user)
    note = (note or "").strip()
    if not note:
        raise ValidationError({"note": "Say what still needs doing."})
    if alert.status not in (RESOLVED, AWAITING):
        raise ValidationError({"detail": "This alert is not resolved."})
    alert.status, alert.resolved_at, alert.reviewed_at, alert.reviewed_by = OPEN, None, None, None
    alert.reminded_at = alert.escalated_at = None
    alert.save()
    log_event(alert, "sent_back", user, note)
    return alert


def to_review():
    """The owner's "To review" list: write-offs waiting for approval, and a manager's resolutions not yet accepted."""
    from django.db.models import Q

    return Alert.objects.filter(Q(status=AWAITING) | Q(status=RESOLVED, reviewed_at__isnull=True, resolved_by__role=Role.MANAGER))


# ---- the hourly job: patterns, reminders, escalation -----------------------------------------------------------

def apply_patterns(now=None):
    """Repeats that point at a cause: the same tank losing fuel three times in a week (often its calibration chart),
    or the same attendant with three cash differences in a week."""
    now = now or timezone.now()
    week = now - timedelta(days=7)
    recent = list(Alert.objects.filter(created_at__gte=week, kind__in=["loss", "cash"]))
    for a in Alert.objects.filter(status__in=[OPEN, AWAITING], kind__in=["loss", "cash"]):
        pattern = ""
        if a.kind == "loss" and a.tank_id:
            n = sum(1 for x in recent if x.kind == "loss" and x.tank_id == a.tank_id)
            if n >= 3:
                pattern = f"{n} fuel losses on this tank in 7 days: check the tank's calibration chart and dips."
        elif a.kind == "cash":
            for name in a.data.get("people", []):
                n = sum(1 for x in recent if x.kind == "cash" and name in x.data.get("people", []))
                if n >= 3:
                    pattern = f"{name}: {n} cash differences in 7 days."
                    break
        if pattern != a.pattern:
            a.pattern = pattern
            a.save(update_fields=["pattern", "updated_at"])
            if pattern:
                log_event(a, "pattern", note=pattern)


def _managers_for(station_id):
    from apps.stations.models import StationAssignment

    managers = list(User.objects.filter(role=Role.MANAGER, is_active=True))
    assigned = set(StationAssignment.objects.filter(station_id=station_id).values_list("user_id", flat=True))
    limited = set(StationAssignment.objects.values_list("user_id", flat=True))
    return [m for m in managers if m.pk in assigned or m.pk not in limited]


def escalate(tenant_id, now=None):
    """Remind the station's manager after 12 hours open; tell the owner after 24. Each happens once per opening."""
    from apps.tenants.services import get_settings

    from .messaging import send_message

    now = now or timezone.now()
    cfg = get_settings(tenant_id)
    channel = cfg.summary_channel if cfg.summary_channel != "none" else "sms"
    out = {"reminded": 0, "escalated": 0}
    for a in Alert.objects.filter(status=OPEN).select_related("station"):
        opened = a.events.filter(kind__in=["opened", "reopened", "sent_back"]).order_by("-created_at").values_list("created_at", flat=True).first() or a.created_at
        age = now - opened
        if age >= REMIND_AFTER and a.reminded_at is None:
            for m in _managers_for(a.station_id):
                send_message(tenant_id=tenant_id, channel=channel, to=m.phone, kind="alert_reminder",
                             body=f"Reminder, open {int(age.total_seconds() // 3600)} h: {a.message} Resolve it in PumpWise, Alerts.")
            a.reminded_at = now
            a.save(update_fields=["reminded_at", "updated_at"])
            log_event(a, "reminded", note="The station's manager was reminded.")
            out["reminded"] += 1
        if age >= ESCALATE_AFTER and a.escalated_at is None:
            send_message(tenant_id=tenant_id, channel=cfg.summary_channel, to=cfg.summary_phone, kind="alert_escalated",
                         body=f"Not resolved after {int(age.total_seconds() // 3600)} h: {a.message}")
            a.escalated_at = now
            a.save(update_fields=["escalated_at", "updated_at"])
            log_event(a, "escalated", note="The owner was told: still open after 24 hours.")
            out["escalated"] += 1
    return out


def summary_lines(day):
    """For the owner's daily message: what managers resolved that day and how, and what is still open."""
    from apps.operations import tradingday

    lines = []
    start, end = tradingday.bounds(None, day)  # the trading day, 21:00 to 21:00
    done = AlertEvent.objects.filter(kind__in=["resolved", "submitted_for_approval"], actor__role=Role.MANAGER,
                                     created_at__gt=start, created_at__lte=end).select_related("alert")
    if done:
        counts = {}
        for e in done:
            label = e.data.get("label") or reason_label(e.alert.kind, e.data.get("reason", ""))
            counts[label] = counts.get(label, 0) + 1
        lines.append(f"Managers resolved {len(done)} alert{'s' if len(done) != 1 else ''}: " + "; ".join(f"{n} {label}" for label, n in counts.items()))
    waiting = Alert.objects.filter(status=AWAITING).count()
    if waiting:
        lines.append(f"Waiting for your approval: {waiting}")
    still = Alert.objects.filter(status=OPEN).order_by("created_at")
    if still:
        oldest = (timezone.now() - still[0].created_at).days
        lines.append(f"Still open: {still.count()}" + (f", the oldest {oldest} day{'s' if oldest != 1 else ''}" if oldest >= 1 else ""))
    return lines
