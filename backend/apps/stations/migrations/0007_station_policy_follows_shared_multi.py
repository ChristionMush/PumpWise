from importlib import import_module

from django.db import migrations, models

OLD_GUARD = import_module("apps.stations.migrations.0006_nozzle_tank_pump_assigned_to").GUARD

# A station a tenant adds itself follows the business's pump system: assigned -> several attendants with assigned
# pumps; shared_multi -> several attendants sharing the pumps. Anything else still needs the platform.
GUARD = OLD_GUARD.replace(
    "AND NOT (TG_OP = 'INSERT' AND sys = 'assigned' AND NEW.attendants_mode = 'multiple' AND NEW.assignment_mode = 'assigned') THEN",
    "AND NOT (TG_OP = 'INSERT' AND NEW.attendants_mode = 'multiple' AND ("
    "(sys = 'assigned' AND NEW.assignment_mode = 'assigned') OR (sys = 'shared_multi' AND NEW.assignment_mode = 'shared'))) THEN",
)
assert GUARD != OLD_GUARD


class Migration(migrations.Migration):
    """Third pump system: shared pumps with several attendants. New stations a tenant adds follow it."""

    dependencies = [
        ('stations', '0006_nozzle_tank_pump_assigned_to'),
        ('tenants', '0010_tenant_pump_system_shared_multi'),
    ]

    operations = [
        migrations.RunSQL(sql=[GUARD], reverse_sql=[OLD_GUARD]),
    ]
