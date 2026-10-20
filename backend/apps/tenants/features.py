"""
Paid feature grants and per-station attendant policy (PLAN.md section 4). Platform owner only.

* Each tenant has two switches: several attendants per shift, and pump assignment (which needs the first).
* The platform sets each station's `attendants_mode` (single/multiple) and `assignment_mode` (shared/assigned),
  and can only choose what the tenant has been granted.
* Removing a grant (for example when unpaid) falls the stations back to one attendant and shared pumps.
  No data is deleted: past shifts keep their attendants and assignments.
* Policy cannot change while a shift is open at the station.
* The pump system is chosen when the business is created: "shared" (one attendant per shift), "shared_multi"
  (several attendants share every pump) or "assigned" (each attendant works their own pumps). It grants what it
  needs and puts every station on its policy; new stations follow it.

The database backs this up: a trigger on stations_station refuses policy changes outside the platform context
and modes the tenant has not been granted (stations migration 0005).
"""
from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.audit import services as audit
from apps.core.db import tenant_scope

SINGLE, MULTIPLE, SHARED, ASSIGNED = "single", "multiple", "shared", "assigned"
SHARED_MULTI = "shared_multi"

# The station policy (attendants_mode, assignment_mode) each pump system puts a station on.
SYSTEM_POLICY = {SHARED: (SINGLE, SHARED), SHARED_MULTI: (MULTIPLE, SHARED), ASSIGNED: (MULTIPLE, ASSIGNED)}


def _open_shift_stations(stations):
    from apps.operations.models import Shift

    ids = set(Shift.objects.filter(station__in=stations, status="open").values_list("station_id", flat=True))
    return [s for s in stations if s.pk in ids]


def set_tenant_features(*, tenant, multi_attendant=None, pump_assignment=None, actor=None, request=None):
    """Grant or remove the paid features. Returns the stations that were moved back to the basic policy."""
    from apps.stations.models import Station

    multi = tenant.multi_attendant_enabled if multi_attendant is None else bool(multi_attendant)
    assign = tenant.pump_assignment_enabled if pump_assignment is None else bool(pump_assignment)
    if multi_attendant is False and pump_assignment is None:
        assign = False  # removing several-attendants also removes pump assignment, which depends on it
    if assign and not multi:
        raise ValidationError({"pump_assignment_enabled": "Pump assignment needs several attendants per shift."})

    before = {"multi_attendant_enabled": tenant.multi_attendant_enabled, "pump_assignment_enabled": tenant.pump_assignment_enabled}
    with transaction.atomic(), tenant_scope(tenant.id):
        stations = list(Station.objects.all())
        fall_back = [s for s in stations if (not multi and s.attendants_mode != SINGLE) or (not assign and s.assignment_mode != SHARED)]
        if busy := _open_shift_stations(fall_back):
            raise ValidationError({"detail": "A shift is open at " + ", ".join(s.name for s in busy) + ". Change this after it closes."})
        tenant.multi_attendant_enabled, tenant.pump_assignment_enabled = multi, assign
        # the pump system never claims a feature the business no longer has
        if not multi:
            tenant.pump_system = SHARED
        elif not assign and tenant.pump_system == ASSIGNED:
            tenant.pump_system = SHARED_MULTI
        tenant.save(update_fields=["multi_attendant_enabled", "pump_assignment_enabled", "pump_system", "updated_at"])
        for s in fall_back:
            s.attendants_mode = s.attendants_mode if multi else SINGLE
            s.assignment_mode = s.assignment_mode if assign else SHARED
            s.save(update_fields=["attendants_mode", "assignment_mode", "updated_at"])
        after = {"multi_attendant_enabled": multi, "pump_assignment_enabled": assign}
        audit.log(
            "tenant_features_set", actor=actor, tenant_id=tenant.id, obj=tenant, request=request,
            changes={**audit.diff(before, after), **({"stations_reset": [s.name for s in fall_back]} if fall_back else {})},
        )
    return fall_back


def set_pump_system(*, tenant, system, actor=None, request=None):
    """The pump system for the whole business: grants what it needs and moves every station to its policy."""
    from apps.stations.models import Station

    if system not in SYSTEM_POLICY:
        raise ValidationError({"pump_system": "Choose shared pumps (one or several attendants) or assigned pumps."})
    mode, assignment = SYSTEM_POLICY[system]
    with transaction.atomic(), tenant_scope(tenant.id):
        stations = list(Station.objects.all())
        changing = [s for s in stations if (s.attendants_mode, s.assignment_mode) != (mode, assignment)]
        if busy := _open_shift_stations(changing):
            raise ValidationError({"detail": "A shift is open at " + ", ".join(s.name for s in busy) + ". Change this after it closes."})
        before = tenant.pump_system
        if mode == MULTIPLE:
            tenant.multi_attendant_enabled = True
        if assignment == ASSIGNED:
            tenant.pump_assignment_enabled = True
        tenant.pump_system = system
        tenant.save(update_fields=["multi_attendant_enabled", "pump_assignment_enabled", "pump_system", "updated_at"])
        for s in changing:
            s.attendants_mode, s.assignment_mode = mode, assignment
            s.save(update_fields=["attendants_mode", "assignment_mode", "updated_at"])
        audit.log("tenant_pump_system_set", actor=actor, tenant_id=tenant.id, obj=tenant, request=request,
                  changes={"pump_system": [before, system], **({"stations": [s.name for s in changing]} if changing else {})})
    return changing


def set_station_policy(*, station, attendants_mode=None, assignment_mode=None, actor=None, request=None):
    """Choose one station's attendant policy, within what its business has been granted."""
    from apps.tenants.models import Tenant

    mode = attendants_mode or station.attendants_mode
    assignment = assignment_mode or station.assignment_mode
    if mode not in (SINGLE, MULTIPLE) or assignment not in (SHARED, ASSIGNED):
        raise ValidationError({"detail": "Unknown policy."})
    if assignment == ASSIGNED and mode != MULTIPLE:
        raise ValidationError({"assignment_mode": "Pump assignment needs several attendants per shift."})
    with transaction.atomic(), tenant_scope(station.tenant_id):
        tenant = Tenant.objects.get(pk=station.tenant_id)
        if mode == MULTIPLE and not tenant.multi_attendant_enabled:
            raise ValidationError({"attendants_mode": "This business has not been granted several attendants per shift."})
        if assignment == ASSIGNED and not tenant.pump_assignment_enabled:
            raise ValidationError({"assignment_mode": "This business has not been granted pump assignment."})
        if (mode, assignment) != (station.attendants_mode, station.assignment_mode) and _open_shift_stations([station]):
            raise ValidationError({"detail": f"A shift is open at {station.name}. Change the policy after it closes."})
        before = {"attendants_mode": station.attendants_mode, "assignment_mode": station.assignment_mode}
        station.attendants_mode, station.assignment_mode = mode, assignment
        station.save(update_fields=["attendants_mode", "assignment_mode", "updated_at"])
        audit.log("station_policy_set", actor=actor, tenant_id=station.tenant_id, obj=station, request=request,
                  changes=audit.diff(before, {"attendants_mode": mode, "assignment_mode": assignment}))
    return station
