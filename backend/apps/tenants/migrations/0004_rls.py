from django.db import migrations

from apps.core.rls import enable_rls, tenant_isolation


class Migration(migrations.Migration):
    """Row-Level Security for the tables created just before (kept separate so every column exists first)."""

    dependencies = [('core', '0001_initial'), ('tenants', '0003_tenantsettings')]

    operations = [
        enable_rls("tenants_tenantsettings", tenant_isolation()),
    ]
