from django.db import migrations

from apps.core.rls import enable_rls, tenant_isolation


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial"), ("exports", "0001_initial")]
    operations = [enable_rls("exports_tenantexport", tenant_isolation())]
