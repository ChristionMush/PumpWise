"""Weekly pump rotation on assigned-pump stations: two crews, one week on and one week off. Each week the crew on has
day people (07:00 to 18:00) and night people (18:00 to 07:00), each on their own pumps.

The manager assigns pumps by hand as usual. While the station's rota is on, every assignment is remembered for the
crew and shift it was made for (PumpRotaHolder): a handover planned while someone is on the pump is for the next
shift; anything else is for the shift starting within the hour, or under way. An hour before every shift change
(06:00 and 17:00) the pumps are switched to whoever that crew had on them last time, and the usual handover passes
each pump over when the outgoing person ends their part. A shift nobody has assigned yet is left alone, and a manual
change made later is learned in its place. The crews swap at 07:00 every 7 days from the rota's start day.
"""
from datetime import datetime, time, timedelta

from django.db import transaction as db_tx
from django.utils import timezone

from apps.audit import services as audit
from apps.stations.models import Pump

from .models import PumpRota, PumpRotaHolder

DAY_STARTS, NIGHT_STARTS = time(7, 0), time(18, 0)
LEAD = timedelta(hours=1)  # pumps switch this long before the shift change, so the incoming people see theirs
WEEK = timedelta(days=7)


def _local(day, at):
    return timezone.make_aware(datetime.combine(day, at), timezone.get_current_timezone())


def shift_at(at):
    """(start, "day" | "night") of the shift under way at this moment. A night belongs to the evening it starts."""
    local = timezone.localtime(at)
    if local.time() < DAY_STARTS:
        return _local(local.date() - timedelta(days=1), NIGHT_STARTS), "night"
    if local.time() < NIGHT_STARTS:
        return _local(local.date(), DAY_STARTS), "day"
    return _local(local.date(), NIGHT_STARTS), "night"


def next_shift(at):
    start, part = shift_at(at)
    return (start + timedelta(hours=11), "night") if part == "day" else (start + timedelta(hours=13), "day")


def crew_of(rota, start):
    """The crew on for the shift starting then: crew A the week from week_starts, crew B the next, and so on."""
    return "ab"[((start - _local(rota.week_starts, DAY_STARTS)) // WEEK) % 2]


def slot_key(start, part):
    return f"{timezone.localtime(start):%Y-%m-%d}:{part}"


def rota_for(station):
    """The station's rota while it is on and the business is in Pesnat mode."""
    from apps.tenants.features import pesnat_on

    if not pesnat_on(station.tenant_id):
        return None
    return PumpRota.objects.filter(station=station, on=True).first()


def learn(*, pump, to_user, at, handover):
    """A manager just gave this pump to to_user: remember it for that crew's shift (no-op without a rota)."""
    rota = rota_for(pump.station)
    if rota is None:
        return None
    start, part = next_shift(at) if handover else shift_at(at + LEAD)
    crew = crew_of(rota, start)
    PumpRotaHolder.objects.update_or_create(tenant_id=rota.tenant_id, rota=rota, pump=pump, crew=crew, part=part, defaults={"user": to_user})
    return crew, part


def planned_holder(rota, pump, at=None):
    """Who the rota puts on this pump for the shift starting within the hour (or under way), if it has learned it."""
    start, part = shift_at((at or timezone.now()) + LEAD)
    row = PumpRotaHolder.objects.filter(rota=rota, pump=pump, crew=crew_of(rota, start), part=part).first()
    return row.user_id if row else None


def apply_rota(rota, at=None):
    """Switch the station's pumps to the crew and shift starting within the hour. Once per shift: a manager's change
    after that stands (and is learned) until the next shift change. Returns the pumps that changed hands."""
    from .services import _works_at  # services imports this module

    start, part = shift_at((at or timezone.now()) + LEAD)
    key = slot_key(start, part)
    if rota.applied_slot == key:
        return []
    crew = crew_of(rota, start)
    changed = []
    with db_tx.atomic():
        for h in PumpRotaHolder.objects.filter(rota=rota, crew=crew, part=part).select_related("user", "pump__assigned_to").order_by("pump__name"):
            pump, person = h.pump, h.user
            if pump.station_id != rota.station_id or pump.assigned_to_id == person.pk:
                continue
            if not person.is_active or person.deleted_at or not _works_at(person, rota.station):
                continue  # left, or moved to another station: the manager chooses someone (the Pumps screen asks)
            before = pump.assigned_to.full_name if pump.assigned_to_id else None
            Pump.objects.filter(pk=pump.pk).update(assigned_to=person, updated_at=timezone.now())
            audit.log("pump_assigned", tenant_id=rota.tenant_id, obj=pump,
                      changes={"pump": pump.name, "from": before, "to": person.full_name, "rota": f"crew {crew.upper()}, {key}"})
            changed.append(pump.name)
        rota.applied_slot = key
        rota.save(update_fields=["applied_slot", "updated_at"])
    return changed


def apply_tenant_rotas(tenant_id, at=None):
    """Hourly (insights.tasks): every station rota of this business (none outside Pesnat mode)."""
    from apps.tenants.features import pesnat_on

    if not pesnat_on(tenant_id):
        return {}
    return {str(r.station_id): apply_rota(r, at) for r in PumpRota.objects.filter(on=True).select_related("station")}


def describe(rota, pumps, at=None):
    """The rota as the Pumps screen shows it: who is on now, and what has been learned for each crew and shift."""
    start, part = shift_at(at or timezone.now())
    holders = {(h.pump_id, h.crew, h.part): h.user for h in PumpRotaHolder.objects.filter(rota=rota).select_related("user")}
    return {
        "on": rota.on, "week_starts": rota.week_starts, "crew_now": crew_of(rota, start), "part_now": part,
        "pumps": [{"id": str(p.pk), "name": p.name,
                   "holders": {f"{c}_{s}": ({"id": str(u.pk), "name": u.full_name} if (u := holders.get((p.pk, c, s))) else None)
                               for c in "ab" for s in ("day", "night")}}
                  for p in pumps],
    }
