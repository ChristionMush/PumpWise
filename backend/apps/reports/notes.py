"""Notes for the books: something the owner wants the bookkeeper to know about a trading day that the system's numbers do
not show, such as fuel sold that was not recorded under the person who sold it. Kept in the audit log (append-only), so
a note cannot be changed or removed once written; it appears in the accounting pack's Notes sheet and in the day log."""
from datetime import date

from apps.audit import services as audit
from apps.audit.models import AuditLog

ACTION = "book_note"


def add_book_note(*, station, day, note, litres=None, amount=None, actor=None, via_platform=None):
    return audit.log(ACTION, actor=actor, tenant_id=station.tenant_id, obj=station, via_platform=via_platform, changes={
        "day": day.isoformat(), "note": note, "litres": str(litres) if litres is not None else None,
        "amount": str(amount) if amount is not None else None, "currency": station.pricing_currency})


def book_notes(station, first, last):
    """The notes for this station's trading days first..last, in day order."""
    rows = AuditLog.objects.filter(action=ACTION, object_type="Station", object_id=str(station.pk)).select_related("actor").order_by("created_at")
    notes = [{**r.changes, "day": date.fromisoformat(r.changes["day"]), "at": r.created_at,
              "by": r.actor.full_name if r.actor_id and not r.via_platform else "PumpWise (at the owner's request)"} for r in rows]
    return sorted((n for n in notes if first <= n["day"] <= last), key=lambda n: n["day"])
