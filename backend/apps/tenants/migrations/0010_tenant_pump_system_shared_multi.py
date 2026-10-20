from django.db import migrations, models

SYSTEMS = [('shared', 'Shared pumps, one attendant per shift'), ('shared_multi', 'Shared pumps, several attendants'), ('assigned', 'Assigned pumps')]


class Migration(migrations.Migration):
    """Third pump system: shared pumps, several attendants. Businesses already granted several attendants without
    pump assignment keep working as they do (their stations are unchanged); only the label of the system moves."""

    dependencies = [
        ('tenants', '0009_tenant_pump_system'),
    ]

    operations = [
        migrations.RemoveConstraint(model_name='tenant', name='tenant_assigned_system_needs_grant'),
        migrations.AlterField(model_name='tenant', name='pump_system', field=models.CharField(choices=SYSTEMS, default='shared', max_length=20)),
        migrations.RunSQL(
            sql=[
                "SET CONSTRAINTS ALL IMMEDIATE",
                "ALTER TABLE tenants_tenant NO FORCE ROW LEVEL SECURITY",
                "UPDATE tenants_tenant SET pump_system = 'shared_multi' WHERE pump_system = 'shared' AND multi_attendant_enabled",
                "ALTER TABLE tenants_tenant FORCE ROW LEVEL SECURITY",
            ],
            reverse_sql=[
                "SET CONSTRAINTS ALL IMMEDIATE",
                "ALTER TABLE tenants_tenant NO FORCE ROW LEVEL SECURITY",
                "UPDATE tenants_tenant SET pump_system = 'shared' WHERE pump_system = 'shared_multi'",
                "ALTER TABLE tenants_tenant FORCE ROW LEVEL SECURITY",
            ],
        ),
        migrations.AddConstraint(
            model_name='tenant',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('pump_system', 'assigned'), _negated=True), ('pump_assignment_enabled', True), _connector='OR'), name='tenant_assigned_system_needs_grant'),
        ),
        migrations.AddConstraint(
            model_name='tenant',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('pump_system', 'shared_multi'), _negated=True), ('multi_attendant_enabled', True), _connector='OR'), name='tenant_multi_system_needs_grant'),
        ),
    ]
