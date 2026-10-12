"""Tenant A can never read, update or delete tenant B's stations or subscription:
through the API, the ORM and raw SQL."""
import pytest
from django.db import DatabaseError, connections, transaction

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.stations.models import Station
from apps.tenants.models import Subscription

from .helpers import client_for, make_station, two_tenants


def _scalar(sql, params=None):
    with connections["default"].cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()[0]


def _setup():
    a, b = two_tenants()
    return a, b, make_station(a["tenant"], "A-One"), make_station(a["tenant"], "A-Two"), make_station(b["tenant"], "B-One")


def test_api_lists_only_own_stations_for_every_tenant_role():
    a, b, *_ = _setup()
    for role in (Role.OWNER, Role.MANAGER, Role.ATTENDANT):
        names = {s["name"] for s in client_for(a[role]).get("/api/stations/").json()["results"]}
        assert names == {"A-One", "A-Two"}
    assert {s["name"] for s in client_for(b[Role.OWNER]).get("/api/stations/").json()["results"]} == {"B-One"}


def test_platform_admin_has_no_direct_station_access():
    from .helpers import platform_client

    _setup()
    assert platform_client().get("/api/stations/").status_code == 403


def test_orm_read_update_delete_other_tenants_station():
    a, b, a1, a2, b1 = _setup()
    with db_context(tenant_id=a["tenant"].id):
        assert set(Station.all_objects.values_list("name", flat=True)) == {"A-One", "A-Two"}
        assert not Station.all_objects.filter(pk=b1.pk).exists()
        assert Station.objects.filter(pk=b1.pk).update(name="HACKED") == 0
        assert Station.objects.filter(pk=b1.pk).delete() == 0  # soft delete: nothing touched
    with db_context(tenant_id=b["tenant"].id):
        fresh = Station.objects.get(pk=b1.pk)
        assert fresh.name == "B-One" and fresh.deleted_at is None


def test_orm_cannot_insert_station_for_other_tenant_or_move_one():
    a, b, a1, a2, b1 = _setup()
    with db_context(tenant_id=a["tenant"].id):
        with pytest.raises(DatabaseError), transaction.atomic():
            Station.objects.create(tenant_id=b["tenant"].id, name="Planted")
        with pytest.raises(DatabaseError), transaction.atomic():
            Station.objects.filter(pk=a1.pk).update(tenant_id=b["tenant"].id)


def test_no_context_sees_no_stations():
    _setup()
    with transaction.atomic():
        assert Station.all_objects.count() == 0


def test_raw_sql_read_update_insert_delete():
    a, b, a1, a2, b1 = _setup()
    with db_context(tenant_id=a["tenant"].id):
        assert _scalar("SELECT count(*) FROM stations_station") == 2
        assert _scalar("SELECT count(*) FROM stations_station WHERE id = %s", [b1.id]) == 0
        with connections["default"].cursor() as cur:
            cur.execute("UPDATE stations_station SET name = 'HACKED' WHERE id = %s", [b1.id])
            assert cur.rowcount == 0
        with pytest.raises(DatabaseError), transaction.atomic(), connections["default"].cursor() as cur:
            cur.execute("DELETE FROM stations_station WHERE id = %s", [a1.id])  # not even own rows: no grant
        with pytest.raises(DatabaseError), transaction.atomic(), connections["default"].cursor() as cur:
            cur.execute(
                "INSERT INTO stations_station (id, created_at, updated_at, tenant_id, name, address, status)"
                " VALUES (gen_random_uuid(), now(), now(), %s, 'Planted', '', 'active')",
                [b["tenant"].id],
            )
    with db_context(tenant_id=b["tenant"].id):
        assert _scalar("SELECT name FROM stations_station") == "B-One"


def test_platform_can_only_reach_stations_by_narrowing_to_one_tenant():
    a, b, *_ = _setup()
    with db_context(platform_admin=True):
        assert Station.all_objects.count() == 0
    with db_context(platform_admin=True, tenant_id=a["tenant"].id):
        assert Station.all_objects.count() == 2


def test_subscription_is_readable_by_its_tenant_only_and_never_writable_by_tenants():
    a, b, *_ = _setup()
    with db_context(tenant_id=a["tenant"].id):
        assert list(Subscription.objects.values_list("tenant_id", flat=True)) == [a["tenant"].id]
        assert Subscription.objects.update(status="cancelled") == 0  # not even its own
        assert Subscription.objects.filter(tenant_id=b["tenant"].id).update(plan="x") == 0
        with pytest.raises(DatabaseError), transaction.atomic():
            Subscription.objects.create(tenant_id=a["tenant"].id)
    with db_context(platform_admin=True):
        assert Subscription.objects.count() == 2
        assert set(Subscription.objects.values_list("status", flat=True)) == {"active"}


def test_station_names_are_unique_per_tenant_but_reusable_across_tenants():
    a, b, *_ = _setup()
    make_station(b["tenant"], "A-One")  # same name, different tenant: fine
    with pytest.raises(DatabaseError):
        make_station(a["tenant"], "a-one")  # same tenant, case-insensitive: refused
