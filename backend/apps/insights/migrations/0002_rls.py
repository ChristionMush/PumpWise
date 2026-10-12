from django.db import migrations

from apps.core.rls import enable_rls, tenant_isolation


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial"), ("insights", "0001_initial")]

    operations = [
        enable_rls("insights_alert", tenant_isolation()),
        enable_rls("insights_outboundmessage", tenant_isolation()),
    ]
