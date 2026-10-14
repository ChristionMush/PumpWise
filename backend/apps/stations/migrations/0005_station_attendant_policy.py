from django.db import migrations, models

# Backstop for the platform-only rule, enforced in the database like RLS:
# * a tenant can never change a station's attendant policy (only the platform can), and
# * nobody can choose a mode the business has not been granted.
# Saves that leave both columns unchanged pass straight through.
GUARD = """
CREATE OR REPLACE FUNCTION station_policy_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    multi boolean;
    assign boolean;
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.attendants_mode IS NOT DISTINCT FROM OLD.attendants_mode
       AND NEW.assignment_mode IS NOT DISTINCT FROM OLD.assignment_mode THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' AND NEW.attendants_mode = 'single' AND NEW.assignment_mode = 'shared' THEN
        RETURN NEW;
    END IF;
    IF NOT app_platform_admin() THEN
        RAISE EXCEPTION 'Only the platform can change a station''s attendant policy' USING ERRCODE = '42501';
    END IF;
    SELECT multi_attendant_enabled, pump_assignment_enabled INTO multi, assign FROM tenants_tenant WHERE id = NEW.tenant_id;
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

TRIGGER = """
CREATE TRIGGER station_policy_guard
BEFORE INSERT OR UPDATE OF attendants_mode, assignment_mode ON stations_station
FOR EACH ROW EXECUTE FUNCTION station_policy_guard()
"""


class Migration(migrations.Migration):
    """Per-station attendant policy (single/multiple, shared/assigned), set by the platform within the tenant's grants."""

    dependencies = [
        ('core', '0001_initial'),
        ('stations', '0004_alter_exchangerate_rate'),
        ('tenants', '0007_tenant_feature_grants'),
    ]

    operations = [
        migrations.AddField(
            model_name='station',
            name='attendants_mode',
            field=models.CharField(choices=[('single', 'One attendant per shift'), ('multiple', 'Several attendants per shift')], default='single', max_length=10),
        ),
        migrations.AddField(
            model_name='station',
            name='assignment_mode',
            field=models.CharField(choices=[('shared', 'Everyone shares the pumps'), ('assigned', 'Each pump is assigned to one attendant')], default='shared', max_length=10),
        ),
        migrations.AddConstraint(
            model_name='station',
            constraint=models.CheckConstraint(condition=models.Q(('assignment_mode', 'shared'), ('attendants_mode', 'multiple'), _connector='OR'), name='station_assigned_needs_multiple'),
        ),
        migrations.RunSQL(
            sql=[GUARD, TRIGGER],
            reverse_sql=[
                "DROP TRIGGER IF EXISTS station_policy_guard ON stations_station",
                "DROP FUNCTION IF EXISTS station_policy_guard()",
            ],
        ),
    ]
