import uuid

import django.db.models.deletion
from django.db import migrations, models

from apps.core.rls import enable_rls, tenant_isolation

# The owner role runs migrations, and FORCE ROW LEVEL SECURITY binds even the owner, so with no tenant context it
# would see no rows at all. The copy therefore lifts FORCE for these tables inside this one migration transaction
# (the owner owns them, so it may), copies, and puts FORCE back before the transaction ends.
TABLES = ("operations_shift", "operations_shiftattendant", "operations_cashsubmission")
# Foreign keys are DEFERRABLE INITIALLY DEFERRED; checking them at once leaves no pending trigger events, which
# Postgres requires before the ALTER TABLE statements that follow in the same transaction.
NO_FORCE = ["SET CONSTRAINTS ALL IMMEDIATE"] + [f'ALTER TABLE "{t}" NO FORCE ROW LEVEL SECURITY' for t in TABLES]
FORCE = [f'ALTER TABLE "{t}" FORCE ROW LEVEL SECURITY' for t in TABLES] + ["SET CONSTRAINTS ALL DEFERRED"]

# Every existing shift's single attendant becomes its lead ShiftAttendant; their cash is attributed to them.
FORWARD = NO_FORCE + [
    """
    INSERT INTO operations_shiftattendant (id, created_at, updated_at, deleted_at, tenant_id, shift_id, user_id, is_lead, joined_at, left_at, cash_submitted_at)
    SELECT gen_random_uuid(), now(), now(), NULL, s.tenant_id, s.id, s.attendant_id, true, s.started_at, NULL,
           CASE WHEN s.status <> 'open' THEN COALESCE(s.ended_at, s.updated_at) END
    FROM operations_shift s
    WHERE s.attendant_id IS NOT NULL
    """,
    """
    UPDATE operations_cashsubmission c SET attendant_id = s.attendant_id
    FROM operations_shift s WHERE c.shift_id = s.id AND c.attendant_id IS NULL
    """,
] + FORCE

# Back to one attendant per shift: the lead (or, failing that, whoever joined first).
REVERSE = NO_FORCE + [
    """
    UPDATE operations_shift s SET attendant_id = a.user_id
    FROM (
        SELECT DISTINCT ON (shift_id) shift_id, user_id FROM operations_shiftattendant
        WHERE deleted_at IS NULL ORDER BY shift_id, is_lead DESC, joined_at
    ) a
    WHERE a.shift_id = s.id
    """,
] + FORCE


class Migration(migrations.Migration):
    """Several attendants per shift (PLAN.md section 4).

    Reversible: going back restores Shift.attendant from the lead. Going back after shifts with several people have
    been recorded fails on purpose if two people handed in the same payment type and currency, rather than merging cash.
    """

    dependencies = [
        ('accounts', '0002_user_must_change_password'),
        ('core', '0001_initial'),
        ('operations', '0004_tank_records_client_id_delivery_photo'),
        ('stations', '0005_station_attendant_policy'),
        ('tenants', '0007_tenant_feature_grants'),
    ]

    operations = [
        migrations.CreateModel(
            name='ShiftAttendant',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('is_lead', models.BooleanField(default=False)),
                ('joined_at', models.DateTimeField()),
                ('left_at', models.DateTimeField(blank=True, null=True)),
                ('cash_submitted_at', models.DateTimeField(blank=True, null=True)),
                ('shift', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='attendants', to='operations.shift')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='tenants.tenant')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user')),
            ],
            options={
                'ordering': ['joined_at'],
                'constraints': [
                    models.UniqueConstraint(models.F('shift'), models.F('user'), condition=models.Q(('deleted_at__isnull', True)), name='shiftattendant_unique_user'),
                    models.UniqueConstraint(models.F('shift'), condition=models.Q(('deleted_at__isnull', True), ('is_lead', True)), name='shiftattendant_one_lead'),
                ],
            },
        ),
        migrations.CreateModel(
            name='PumpAssignment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('started_at', models.DateTimeField()),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('assigned_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user')),
                ('pump', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='stations.pump')),
                ('shift', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='assignments', to='operations.shift')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='tenants.tenant')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user')),
            ],
            options={
                'ordering': ['started_at'],
                'constraints': [
                    models.UniqueConstraint(models.F('shift'), models.F('pump'), condition=models.Q(('deleted_at__isnull', True), ('ended_at__isnull', True)), name='pumpassignment_one_open'),
                ],
            },
        ),
        enable_rls("operations_shiftattendant", tenant_isolation()),
        enable_rls("operations_pumpassignment", tenant_isolation()),
        migrations.AddField(
            model_name='shift',
            name='close_requested_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='shift',
            name='closed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user'),
        ),
        migrations.AddField(
            model_name='cashsubmission',
            name='attendant',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user'),
        ),
        migrations.AddField(
            model_name='transaction',
            name='attendant',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user'),
        ),
        migrations.RemoveConstraint(
            model_name='cashsubmission',
            name='cash_unique_per_shift',
        ),
        migrations.AddConstraint(
            model_name='cashsubmission',
            constraint=models.UniqueConstraint(models.F('shift'), models.F('attendant'), models.F('payment_type'), models.F('currency'), condition=models.Q(('deleted_at__isnull', True)), name='cash_unique_per_attendant'),
        ),
        migrations.RemoveConstraint(
            model_name='pumpmeterreading',
            name='reading_unique_start_end',
        ),
        migrations.AddConstraint(
            model_name='pumpmeterreading',
            constraint=models.UniqueConstraint(models.F('shift'), models.F('nozzle'), models.F('kind'), models.F('recorded_by'), condition=models.Q(('deleted_at__isnull', True), models.Q(('kind', 'checkpoint'), _negated=True)), name='reading_unique_per_person'),
        ),
        migrations.AlterField(
            model_name='shift',
            name='attendant',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user'),
        ),
        migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE),
        migrations.RemoveField(
            model_name='shift',
            name='attendant',
        ),
    ]
