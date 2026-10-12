"""
Helpers to put Row-Level Security on a table from a migration.

Every table gets:  ENABLE + FORCE ROW LEVEL SECURITY  and explicit policies.
FORCE means even the table owner (app_owner) is subject to the policies.

Usage in a migration (after the CreateModel):

    from apps.core.rls import enable_rls, tenant_isolation

    operations = [
        migrations.CreateModel(...),
        enable_rls("stations_station", tenant_isolation()),
    ]

Add ``dependencies = [("core", "0001_initial")]`` so the SQL helper functions exist.
"""
from dataclasses import dataclass

from django.db import migrations

TENANT = "(SELECT app_tenant_id())"
PLATFORM = "(SELECT app_platform_admin())"
LOGIN_ID = "(SELECT app_login_identifier())"


@dataclass(frozen=True)
class Policy:
    name: str
    command: str  # ALL | SELECT | INSERT | UPDATE | DELETE
    using: str | None = None
    check: str | None = None


def tenant_isolation(column="tenant_id", platform_access=False):
    """The standard policy for tenant-owned tables: a row is visible/writable only when
    its tenant_id equals the current tenant. platform_access=True is for control-plane
    tables only; never use it on business data."""
    expr = f"{column} = {TENANT}"
    if platform_access:
        expr += f" OR {PLATFORM}"
    return [Policy("tenant_isolation", "ALL", using=expr, check=expr)]


def _create_policy(table, p):
    sql = f'CREATE POLICY {p.name} ON "{table}" FOR {p.command}'
    if p.using:
        sql += f" USING ({p.using})"
    if p.check:
        sql += f" WITH CHECK ({p.check})"
    return sql


def enable_rls(table, policies):
    forward = [
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
        *[_create_policy(table, p) for p in policies],
    ]
    reverse = [
        *[f'DROP POLICY IF EXISTS {p.name} ON "{table}"' for p in policies],
        f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY',
    ]
    return migrations.RunSQL(sql=forward, reverse_sql=reverse)


def replace_policies(table, old, new):
    """Swap a table's policies in a later migration (reversible)."""
    forward = [f'DROP POLICY IF EXISTS {p.name} ON "{table}"' for p in old] + [_create_policy(table, p) for p in new]
    reverse = [f'DROP POLICY IF EXISTS {p.name} ON "{table}"' for p in new] + [_create_policy(table, p) for p in old]
    return migrations.RunSQL(sql=forward, reverse_sql=reverse)
