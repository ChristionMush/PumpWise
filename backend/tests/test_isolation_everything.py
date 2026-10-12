"""One test that holds EVERY tenant table to the isolation promise, with realistic data in every table.

Rather than trusting per-feature tests to cover new tables, this walks the live database: for each table with a
tenant_id, tenant A must see none of tenant B's rows, must not be able to change them, and even the table OWNER
(which holds DELETE rights) is blocked by FORCE ROW LEVEL SECURITY.
"""
import pytest
from django.db import DatabaseError, connections, transaction

from apps.core.db import db_context
from apps.onboarding.demo import seed_demo_tenant

PLATFORM_ONLY = {"billing_paymentevent"}
# Tables the demo does not fill (they hold events that only exist in live operation). Everything else MUST have rows,
# so a new table with no seed data fails here and gets covered.
# Tenants may READ these but only platform staff may write them, so there is no "own rows are updatable" control for them.
PLATFORM_WRITE = {"billing_payment", "billing_billingreminder", "tenants_subscription"}
EMPTY_OK = {"billing_billingreminder", "operations_syncoperation", "exports_tenantexport", "insights_outboundmessage", "stations_stationassignment"}


@pytest.fixture
def two_demo_tenants():
    a = seed_demo_tenant(name="Iso Alpha", days=4, seed=1, password="Private-demo-pass-9")
    b = seed_demo_tenant(name="Iso Beta", days=4, seed=2, password="Private-demo-pass-9")
    return a["tenant_id"], b["tenant_id"]


def tenant_tables():
    with connections["default"].cursor() as cur:
        cur.execute("SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='tenant_id' ORDER BY 1")
        return [r[0] for r in cur.fetchall() if r[0] not in PLATFORM_ONLY]


def count(sql, params=()):
    with connections["default"].cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def test_every_tenant_table_is_isolated_with_data_present(two_demo_tenants):
    a, b = two_demo_tenants
    tables = tenant_tables()
    assert len(tables) >= 25
    empty = []
    for t in tables:
        with db_context(tenant_id=b):
            b_rows = count(f'SELECT count(*) FROM "{t}"')
        if not b_rows:
            empty.append(t)
    assert set(empty) <= EMPTY_OK, f"These tables have no seed data, so isolation is untested for them: {sorted(set(empty) - EMPTY_OK)}"

    for t in tables:
        with db_context(tenant_id=a):
            mine = count(f'SELECT count(*) FROM "{t}"')
            assert count(f'SELECT count(*) FROM "{t}" WHERE tenant_id <> %s', [a]) == 0, f"{t}: tenant A can read other tenants' rows"
            assert count(f'SELECT count(*) FROM "{t}" WHERE tenant_id = %s', [b]) == 0, f"{t}: tenant A can read B's rows"
            can_update = count("SELECT has_table_privilege(current_user, %s, 'UPDATE')", [t])
            if can_update:
                with connections["default"].cursor() as cur:
                    cur.execute(f'UPDATE "{t}" SET tenant_id = tenant_id WHERE tenant_id = %s', [b])
                    assert cur.rowcount == 0, f"{t}: tenant A changed B's rows"
                    try:  # and cannot hand its rows to B by re-pointing tenant_id: refused outright, or nothing changes
                        with transaction.atomic():
                            cur.execute(f'UPDATE "{t}" SET tenant_id = %s', [b])
                            assert cur.rowcount == 0, f"{t}: tenant A re-pointed rows to another tenant"
                    except DatabaseError:
                        pass
        if can_update and mine and t not in PLATFORM_WRITE:
            with db_context(tenant_id=b):  # positive control: the same statement DOES reach B's own rows, so the test is not vacuous
                with connections["default"].cursor() as cur:
                    cur.execute(f'UPDATE "{t}" SET tenant_id = tenant_id WHERE tenant_id = %s', [b])
                    assert cur.rowcount > 0 or t in EMPTY_OK, t
        # even the owner role (which may DELETE) is blocked from another tenant's rows by FORCE RLS
        with transaction.atomic(using="owner"):
            with connections["owner"].cursor() as cur:
                cur.execute("SELECT set_config('app.tenant_id', %s, true)", [str(a)])
                cur.execute(f'DELETE FROM "{t}" WHERE tenant_id = %s', [b])
                assert cur.rowcount == 0, f"{t}: owner role could delete another tenant's rows"
            transaction.set_rollback(True, using="owner")


def test_with_no_context_every_tenant_table_is_empty(two_demo_tenants):
    for t in tenant_tables():
        with transaction.atomic():
            assert count(f'SELECT count(*) FROM "{t}"') == 0, t


def test_the_runtime_role_can_never_delete_anywhere(two_demo_tenants):
    a, _ = two_demo_tenants
    with db_context(tenant_id=a):
        for t in tenant_tables():
            assert count("SELECT has_table_privilege(current_user, %s, 'DELETE')", [t]) is False, t
