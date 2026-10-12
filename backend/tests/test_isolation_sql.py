"""Same guarantees with RAW SQL, as the real runtime role - no ORM involved."""
import pytest
from django.db import DatabaseError, connections, transaction

from apps.accounts.models import Role
from apps.core.db import db_context, set_db_context

from .helpers import two_tenants


def _scalar(sql, params=None, using="default"):
    with connections[using].cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchone()[0]


def test_runtime_role_is_not_superuser_and_cannot_bypass_rls():
    with connections["default"].cursor() as cur:
        cur.execute("SELECT current_user, rolsuper, rolbypassrls, rolcreaterole FROM pg_roles WHERE rolname = current_user")
        user, superuser, bypass, createrole = cur.fetchone()
    assert (user, superuser, bypass, createrole) == ("app_user", False, False, False)


def test_select_only_sees_own_rows():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        assert _scalar("SELECT count(*) FROM accounts_user") == 3
        assert _scalar("SELECT count(*) FROM accounts_user WHERE id = %s", [b[Role.OWNER].id]) == 0
        assert _scalar("SELECT count(*) FROM accounts_user WHERE tenant_id = %s", [b["tenant"].id]) == 0
        assert _scalar("SELECT count(*) FROM tenants_tenant") == 1


def test_update_cannot_touch_other_tenant():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        with connections["default"].cursor() as cur:
            cur.execute("UPDATE accounts_user SET full_name = 'HACKED' WHERE id = %s", [b[Role.OWNER].id])
            assert cur.rowcount == 0
            cur.execute("UPDATE accounts_user SET full_name = 'HACKED'")
            assert cur.rowcount == 3
    with db_context(tenant_id=b["tenant"].id):
        assert _scalar("SELECT count(*) FROM accounts_user WHERE full_name = 'HACKED'") == 0


def test_runtime_role_cannot_delete_at_all():
    """Defence in depth for 'never hard-delete': no DELETE grant, so even own rows are safe."""
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        with pytest.raises(DatabaseError):
            with transaction.atomic():
                with connections["default"].cursor() as cur:
                    cur.execute("DELETE FROM accounts_user WHERE id = %s", [a[Role.MANAGER].id])


def test_delete_policy_blocks_other_tenant_even_for_the_table_owner():
    """FORCE ROW LEVEL SECURITY: even app_owner (which HAS delete rights) is bound by the policy."""
    a, b = two_tenants()
    with transaction.atomic(using="owner"):
        with connections["owner"].cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", [str(a["tenant"].id)])
            cur.execute("DELETE FROM accounts_user WHERE id = %s", [b[Role.OWNER].id])
            assert cur.rowcount == 0
            cur.execute("SELECT count(*) FROM accounts_user")
            assert cur.fetchone()[0] == 3
    with db_context(tenant_id=b["tenant"].id):
        assert _scalar("SELECT count(*) FROM accounts_user") == 3


def test_owner_role_with_no_context_sees_nothing():
    two_tenants()
    assert _scalar("SELECT count(*) FROM accounts_user", using="owner") == 0


def test_insert_with_other_tenant_id_is_rejected():
    a, b = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        with pytest.raises(DatabaseError):
            with transaction.atomic():
                with connections["default"].cursor() as cur:
                    cur.execute(
                        "INSERT INTO accounts_user (id, created_at, updated_at, password, tenant_id, role, full_name, email,"
                        " preferred_language, is_active, token_version) VALUES (gen_random_uuid(), now(), now(), 'x', %s,"
                        " 'owner', 'Evil', 'evil@example.com', 'en', true, 0)",
                        [b["tenant"].id],
                    )


def test_tenant_user_cannot_become_platform_admin_even_in_raw_sql():
    a, _ = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        for stmt in (
            "UPDATE accounts_user SET role = 'platform_admin' WHERE id = %s",  # CHECK constraint
            "UPDATE accounts_user SET tenant_id = NULL WHERE id = %s",  # RLS WITH CHECK
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                with connections["default"].cursor() as cur:
                    cur.execute(stmt, [a[Role.MANAGER].id])


def test_context_is_transaction_local_and_never_leaks():
    a, _ = two_tenants()
    with db_context(tenant_id=a["tenant"].id):
        assert _scalar("SELECT app_tenant_id()") == a["tenant"].id
    assert _scalar("SELECT app_tenant_id()") is None
    assert _scalar("SELECT app_platform_admin()") is False
    assert _scalar("SELECT count(*) FROM accounts_user") == 0


def test_setting_context_outside_a_transaction_is_refused():
    with pytest.raises(RuntimeError):
        set_db_context(tenant_id="00000000-0000-0000-0000-000000000000")


def test_login_identifier_exposes_exactly_one_account():
    a, b = two_tenants()
    target = b[Role.ATTENDANT]
    with transaction.atomic():
        set_db_context(login_identifier=target.email)
        assert _scalar("SELECT count(*) FROM accounts_user") == 1
        assert _scalar("SELECT id FROM accounts_user") == target.id
        assert _scalar("SELECT count(*) FROM tenants_tenant") == 0
