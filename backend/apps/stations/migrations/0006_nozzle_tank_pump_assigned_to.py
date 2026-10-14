from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models

OLD_GUARD = import_module("apps.stations.migrations.0005_station_attendant_policy").GUARD

# Each nozzle draws from its own tank (a pump has two nozzles on two different tanks), so the tank moves from the
# pump to the nozzle. Existing nozzles take their pump's tank. FORCE ROW LEVEL SECURITY binds even the owner role
# that runs migrations, so it is lifted for the copy inside this transaction and put back straight after.
TABLES = ("stations_pump", "stations_nozzle")
NO_FORCE = ["SET CONSTRAINTS ALL IMMEDIATE"] + [f'ALTER TABLE "{t}" NO FORCE ROW LEVEL SECURITY' for t in TABLES]
FORCE = [f'ALTER TABLE "{t}" FORCE ROW LEVEL SECURITY' for t in TABLES] + ["SET CONSTRAINTS ALL DEFERRED"]
FORWARD = NO_FORCE + ["UPDATE stations_nozzle n SET tank_id = p.tank_id FROM stations_pump p WHERE n.pump_id = p.id AND n.tank_id IS NULL"] + FORCE
REVERSE = NO_FORCE + [
    """
    UPDATE stations_pump p SET tank_id = n.tank_id
    FROM (SELECT DISTINCT ON (pump_id) pump_id, tank_id FROM stations_nozzle ORDER BY pump_id, number) n
    WHERE n.pump_id = p.id
    """,
] + FORCE

# A new station follows the pump system the platform chose for its business (tenants.pump_system), so an owner who
# adds a station to an assigned-pump business gets an assigned-pump station. Everything else is as before: only the
# platform changes a station's policy, and never beyond what the business has been granted.
GUARD = """
CREATE OR REPLACE FUNCTION station_policy_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    multi boolean;
    assign boolean;
    sys text;
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.attendants_mode IS NOT DISTINCT FROM OLD.attendants_mode
       AND NEW.assignment_mode IS NOT DISTINCT FROM OLD.assignment_mode THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.attendants_mode = 'single' AND NEW.assignment_mode = 'shared' THEN
        RETURN NEW;
    END IF;
    SELECT multi_attendant_enabled, pump_assignment_enabled, pump_system INTO multi, assign, sys FROM tenants_tenant WHERE id = NEW.tenant_id;
    IF NOT app_platform_admin()
       AND NOT (TG_OP = 'INSERT' AND sys = 'assigned' AND NEW.attendants_mode = 'multiple' AND NEW.assignment_mode = 'assigned') THEN
        RAISE EXCEPTION 'Only the platform can change a station''s attendant policy' USING ERRCODE = '42501';
    END IF;
    IF NEW.attendants_mode = 'multiple' AND NOT COALESCE(multi, false) THEN
        RAISE EXCEPTION 'This business has not been granted several attendants per shift' USING ERRCODE = '23514';
    END IF;
    IF NEW.assignment_mode = 'assigned' AND NOT COALESCE(assign, false) THEN
        RAISE EXCEPTION 'This business has not been granted pump assignment' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$
"""


class Migration(migrations.Migration):
    """Nozzles draw from tanks (two per pump, separate tanks); pumps are assigned to attendants by managers."""

    dependencies = [
        ('accounts', '0002_user_must_change_password'),
        ('stations', '0005_station_attendant_policy'),
        ('tenants', '0009_tenant_pump_system'),
    ]

    operations = [
        migrations.AddField(
            model_name='nozzle',
            name='tank',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='nozzles', to='stations.tank'),
        ),
        migrations.AlterField(
            model_name='pump',
            name='tank',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='pumps', to='stations.tank'),
        ),
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
        migrations.AlterField(
            model_name='nozzle',
            name='tank',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='nozzles', to='stations.tank'),
        ),
        migrations.RemoveField(
            model_name='pump',
            name='tank',
        ),
        migrations.AddField(
            model_name='pump',
            name='assigned_to',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user'),
        ),
        migrations.RunSQL(sql=[GUARD], reverse_sql=[OLD_GUARD]),
    ]
