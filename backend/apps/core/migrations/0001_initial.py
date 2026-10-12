from django.db import migrations

FORWARD = [
    """
    CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS uuid
    LANGUAGE sql STABLE AS
    $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$
    """,
    """
    CREATE OR REPLACE FUNCTION app_platform_admin() RETURNS boolean
    LANGUAGE sql STABLE AS
    $$ SELECT COALESCE(NULLIF(current_setting('app.platform_admin', true), ''), 'off') = 'on' $$
    """,
    """
    CREATE OR REPLACE FUNCTION app_login_identifier() RETURNS text
    LANGUAGE sql STABLE AS
    $$ SELECT NULLIF(current_setting('app.login_identifier', true), '') $$
    """,
]

REVERSE = [
    "DROP FUNCTION IF EXISTS app_login_identifier()",
    "DROP FUNCTION IF EXISTS app_platform_admin()",
    "DROP FUNCTION IF EXISTS app_tenant_id()",
]


class Migration(migrations.Migration):
    """SQL helper functions used by every RLS policy.

    NULLIF matters: after a SET LOCAL ends, Postgres leaves the setting as '' (not NULL).
    Without it, ''::uuid would raise instead of safely matching nothing.
    """

    initial = True
    dependencies = []
    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
