from django.db import migrations, models


class Migration(migrations.Migration):
    """The pump system (shared or assigned) the platform chooses when it creates a business. Businesses that already
    have pump assignment granted start as "assigned"."""

    dependencies = [
        ('tenants', '0008_tenant_is_demo_trial'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='pump_system',
            field=models.CharField(choices=[('shared', 'Shared pumps'), ('assigned', 'Assigned pumps')], default='shared', max_length=10),
        ),
        migrations.RunSQL(
            sql=[
                "SET CONSTRAINTS ALL IMMEDIATE",
                "ALTER TABLE tenants_tenant NO FORCE ROW LEVEL SECURITY",
                "UPDATE tenants_tenant SET pump_system = 'assigned' WHERE pump_assignment_enabled",
                "ALTER TABLE tenants_tenant FORCE ROW LEVEL SECURITY",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddConstraint(
            model_name='tenant',
            constraint=models.CheckConstraint(condition=models.Q(('pump_system', 'shared'), ('pump_assignment_enabled', True), _connector='OR'), name='tenant_assigned_system_needs_grant'),
        ),
    ]
