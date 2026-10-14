from django.db import migrations, models


class Migration(migrations.Migration):
    """Paid feature grants: several attendants per shift, and pump assignment (which needs the first)."""

    dependencies = [
        ('tenants', '0006_tenantsettings_onboarding_flags'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='multi_attendant_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='tenant',
            name='pump_assignment_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name='tenant',
            constraint=models.CheckConstraint(condition=models.Q(('pump_assignment_enabled', False), ('multi_attendant_enabled', True), _connector='OR'), name='tenant_assignment_needs_multi'),
        ),
    ]
