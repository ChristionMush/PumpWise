"""Paid feature grants and per-station attendant policy: platform owner only, within the grants, enforced in the database."""
import pytest
from django.db import DatabaseError, connections, transaction

from apps.accounts.models import Role
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.operations.models import Shift
from apps.stations.models import Station
from apps.tenants.models import Tenant

from .helpers import at, client_for, make_forecourt, platform_client, run_shift, two_tenants


def features(c, tenant, **body):
    return c.patch(f"/api/platform/tenants/{tenant.id}/features/", body, format="json")


def policy(c, tenant, station, **body):
    return c.patch(f"/api/platform/tenants/{tenant.id}/stations/{station.pk}/policy/", body, format="json")


def station_row(tenant, station):
    with db_context(tenant_id=tenant.id):
        return Station.objects.get(pk=station.pk)


def test_new_tenants_and_stations_start_on_the_basic_policy():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    st = station_row(a["tenant"], fc.station)
    assert (st.attendants_mode, st.assignment_mode) == ("single", "shared")
    d = platform_client().get(f"/api/platform/tenants/{a['tenant'].id}/").json()
    assert d["features"] == {"multi_attendant_enabled": False, "pump_assignment_enabled": False}


def test_platform_grants_features_then_sets_a_station_policy_within_them():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    c = platform_client()
    assert policy(c, a["tenant"], fc.station, attendants_mode="multiple").status_code == 400  # not granted yet
    assert features(c, a["tenant"], pump_assignment_enabled=True).status_code == 400  # assignment needs multi
    assert features(c, a["tenant"], multi_attendant_enabled=True).status_code == 200
    assert policy(c, a["tenant"], fc.station, attendants_mode="multiple", assignment_mode="assigned").status_code == 400  # assignment not granted
    assert policy(c, a["tenant"], fc.station, attendants_mode="multiple").json()["attendants_mode"] == "multiple"
    assert features(c, a["tenant"], pump_assignment_enabled=True).json()["features"]["pump_assignment_enabled"] is True
    assert policy(c, a["tenant"], fc.station, assignment_mode="assigned").json()["assignment_mode"] == "assigned"
    assert policy(c, a["tenant"], fc.station, attendants_mode="single").status_code == 400  # assigned needs multiple
    with db_context(tenant_id=a["tenant"].id):
        actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"tenant_features_set", "station_policy_set"} <= actions


def test_a_station_policy_can_be_chosen_when_the_platform_adds_the_station():
    a, _ = two_tenants()
    c = platform_client()
    features(c, a["tenant"], multi_attendant_enabled=True)
    res = c.post(f"/api/platform/tenants/{a['tenant'].id}/stations/", {"name": "Busy Road", "attendants_mode": "multiple"}, format="json")
    assert res.status_code == 201 and res.json()["attendants_mode"] == "multiple"
    refused = c.post(f"/api/platform/tenants/{a['tenant'].id}/stations/", {"name": "Quiet Road", "assignment_mode": "assigned"}, format="json")
    assert refused.status_code == 400
    with db_context(tenant_id=a["tenant"].id):
        assert not Station.objects.filter(name="Quiet Road").exists()  # atomic: nothing half-created


def test_only_the_platform_can_grant_or_set_policy():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"])
    for role in (Role.OWNER, Role.MANAGER, Role.ATTENDANT):
        c = client_for(a[role])
        assert features(c, a["tenant"], multi_attendant_enabled=True).status_code == 403
        assert policy(c, a["tenant"], fc.station, attendants_mode="multiple").status_code == 403
    # the owner's own station endpoint ignores the policy fields (read-only)
    owner = client_for(a[Role.OWNER])
    res = owner.patch(f"/api/stations/{fc.station.pk}/", {"attendants_mode": "multiple", "assignment_mode": "assigned", "name": "Renamed"}, format="json")
    assert res.status_code == 200 and res.json()["name"] == "Renamed" and res.json()["attendants_mode"] == "single"
    assert features(platform_client(), b["tenant"], multi_attendant_enabled=True).status_code == 200
    assert station_row(a["tenant"], fc.station).attendants_mode == "single"


def test_removing_a_grant_falls_stations_back_without_deleting_anything():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00")
    c = platform_client()
    features(c, a["tenant"], multi_attendant_enabled=True, pump_assignment_enabled=True)
    policy(c, a["tenant"], fc.station, attendants_mode="multiple", assignment_mode="assigned")
    assert client_for(a[Role.MANAGER]).post(f"/api/pumps/{fc.pump.pk}/assign/", {"user": str(a[Role.ATTENDANT].pk)}, format="json").status_code == 200
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [10, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14))
    res = features(c, a["tenant"], multi_attendant_enabled=False)  # e.g. unpaid
    assert res.status_code == 200 and res.json()["stations_reset"] == [fc.station.name]
    assert res.json()["features"] == {"multi_attendant_enabled": False, "pump_assignment_enabled": False}
    st = station_row(a["tenant"], fc.station)
    assert (st.attendants_mode, st.assignment_mode) == ("single", "shared")
    with db_context(tenant_id=a["tenant"].id):
        assert Shift.objects.count() == 1  # history kept


def test_policy_cannot_change_while_a_shift_is_open():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    c = platform_client()
    features(c, a["tenant"], multi_attendant_enabled=True)
    att = client_for(a[Role.ATTENDANT])
    att.post("/api/shifts/start/", {"station": str(fc.station.pk), "readings": [{"nozzle": str(n.pk), "reading": "0"} for n in fc.nozzles]}, format="json")
    res = policy(c, a["tenant"], fc.station, attendants_mode="multiple")
    assert res.status_code == 400 and "open" in str(res.json())


def _raw(tenant_id, sql, params=(), platform=False):
    with transaction.atomic():
        with db_context(tenant_id=tenant_id, platform_admin=platform):
            with connections["default"].cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount


def test_a_tenant_cannot_grant_itself_features_even_with_raw_sql():
    a, b = two_tenants()
    assert _raw(a["tenant"].id, "UPDATE tenants_tenant SET multi_attendant_enabled = true") == 0
    assert _raw(a["tenant"].id, "UPDATE tenants_tenant SET multi_attendant_enabled = true WHERE id = %s", [b["tenant"].id]) == 0
    with db_context(platform_admin=True):
        assert not Tenant.objects.filter(multi_attendant_enabled=True).exists()


def test_a_tenant_cannot_switch_its_station_policy_even_with_raw_sql():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    with pytest.raises(DatabaseError):
        _raw(a["tenant"].id, "UPDATE stations_station SET attendants_mode = 'multiple' WHERE id = %s", [fc.station.pk])
    with pytest.raises(DatabaseError):
        _raw(a["tenant"].id, "INSERT INTO stations_station (id, tenant_id, name, address, status, pricing_currency, attendants_mode, assignment_mode, created_at, updated_at)"
                             " VALUES (gen_random_uuid(), %s, 'Sneaky', '', 'active', 'USD', 'multiple', 'shared', now(), now())", [a["tenant"].id])
    # ordinary edits by the tenant still work (the guard only looks at the policy columns)
    assert _raw(a["tenant"].id, "UPDATE stations_station SET name = 'Renamed' WHERE id = %s", [fc.station.pk]) == 1


def test_even_the_platform_cannot_set_a_mode_that_was_not_granted():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    with pytest.raises(DatabaseError):
        _raw(a["tenant"].id, "UPDATE stations_station SET attendants_mode = 'multiple' WHERE id = %s", [fc.station.pk], platform=True)
    with pytest.raises(DatabaseError):  # and the CHECK keeps assignment tied to several attendants
        _raw(a["tenant"].id, "UPDATE stations_station SET assignment_mode = 'assigned' WHERE id = %s", [fc.station.pk], platform=True)
