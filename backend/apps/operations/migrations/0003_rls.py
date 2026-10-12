from django.db import migrations

from apps.core.rls import enable_rls, tenant_isolation


class Migration(migrations.Migration):
    """Row-Level Security for the tables created just before (kept separate so every column exists first)."""

    dependencies = [('core', '0001_initial'), ('operations', '0002_initial')]

    operations = [
        enable_rls("operations_shift", tenant_isolation()),
        enable_rls("operations_pumpmeterreading", tenant_isolation()),
        enable_rls("operations_tankreading", tenant_isolation()),
        enable_rls("operations_delivery", tenant_isolation()),
        enable_rls("operations_cashsubmission", tenant_isolation()),
        enable_rls("operations_transaction", tenant_isolation()),
        enable_rls("operations_syncoperation", tenant_isolation()),
    ]
