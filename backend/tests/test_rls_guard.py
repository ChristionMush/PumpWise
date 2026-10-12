"""Guard rails: a future phase cannot forget RLS, and the runtime role can never hard-delete."""
from django.db import connections

GLOBAL_TABLES = {"django_migrations"}
# Tables that carry a tenant_id for reference but are PLATFORM-ONLY by design (tenants never read them).
# Adding a table here is a security decision: it must be locked to app_platform_admin() (checked below).
PLATFORM_ONLY = {"billing_paymentevent"}


def _rows(sql):
    with connections["default"].cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def test_every_table_has_forced_rls_and_a_policy():
    tables = _rows(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'"
    )
    assert tables, "no tables found"
    missing = [t for t, enabled, forced in tables if t not in GLOBAL_TABLES and not (enabled and forced)]
    assert not missing, f"Tables without ENABLE+FORCE row level security: {missing}. Use apps.core.rls.enable_rls() in the migration."

    with_policy = {t for (t,) in _rows("SELECT DISTINCT tablename FROM pg_policies WHERE schemaname = 'public'")}
    no_policy = [t for t, *_ in tables if t not in GLOBAL_TABLES and t not in with_policy]
    assert not no_policy, f"RLS enabled but no policy (table unusable): {no_policy}"


def test_every_tenant_id_column_is_covered_by_a_policy_mentioning_it():
    cols = {t for (t,) in _rows("SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='tenant_id'")}
    policies = _rows("SELECT tablename, coalesce(qual,'') || coalesce(with_check,'') FROM pg_policies WHERE schemaname='public'")
    covered = {t for t, expr in policies if "app_tenant_id" in expr}
    assert (cols - PLATFORM_ONLY) <= covered, f"tenant_id tables whose policies do not use app_tenant_id(): {cols - PLATFORM_ONLY - covered}"


def test_platform_only_tables_really_are_locked_to_the_platform():
    for table in PLATFORM_ONLY:
        rows = _rows(f"SELECT cmd, coalesce(qual,''), coalesce(with_check,'') FROM pg_policies WHERE schemaname='public' AND tablename='{table}'")
        assert rows, table
        for cmd, qual, check in rows:
            assert "app_platform_admin" in qual + check and "true" not in (qual + check).replace("app_platform_admin", ""), (table, cmd, qual, check)


def test_runtime_role_has_no_delete_or_truncate_anywhere():
    rows = _rows(
        "SELECT tablename, has_table_privilege(current_user, quote_ident(tablename), 'DELETE'),"
        " has_table_privilege(current_user, quote_ident(tablename), 'TRUNCATE') FROM pg_tables WHERE schemaname='public'"
    )
    offenders = [t for t, d, tr in rows if d or tr]
    assert not offenders, f"app_user must never be able to hard-delete: {offenders}"


def test_audit_table_is_append_only_for_runtime_role():
    (can_update,) = _rows("SELECT has_table_privilege(current_user, 'audit_auditlog', 'UPDATE')")[0]
    assert can_update is False
