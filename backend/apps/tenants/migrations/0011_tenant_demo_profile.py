from django.db import migrations, models


class Migration(migrations.Migration):
    """Which demo data a demo business is reloaded with ("" standard, "presentation")."""

    dependencies = [
        ('tenants', '0010_tenant_pump_system_shared_multi'),
    ]

    operations = [
        migrations.AddField(model_name='tenant', name='demo_profile', field=models.CharField(blank=True, default='', max_length=20)),
    ]
