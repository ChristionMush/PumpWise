import uuid

import django.db.models.deletion
from django.db import migrations, models

from apps.core.rls import enable_rls, tenant_isolation


class Migration(migrations.Migration):
    """Rule 9: rollup tables written when a shift closes (and when a dip or delivery is saved). Dashboards read these."""

    dependencies = [
        ('core', '0001_initial'),
        ('operations', '0005_multi_attendant_shifts'),
        ('stations', '0005_station_attendant_policy'),
        ('tenants', '0007_tenant_feature_grants'),
    ]

    operations = [
        migrations.CreateModel(
            name='DailySalesRollup',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('day', models.DateField()),
                ('currency', models.CharField(max_length=3)),
                ('litres', models.DecimalField(decimal_places=3, max_digits=14)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=16)),
                ('fuel_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='stations.fueltype')),
                ('station', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='stations.station')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='tenants.tenant')),
            ],
            options={
                'indexes': [models.Index(fields=['station', 'day'], name='dailysales_station_day')],
                'constraints': [models.UniqueConstraint(models.F('station'), models.F('day'), models.F('fuel_type'), models.F('currency'), condition=models.Q(('deleted_at__isnull', True)), name='dailysales_unique')],
            },
        ),
        migrations.CreateModel(
            name='TankStatus',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('data', models.JSONField(default=dict)),
                ('computed_at', models.DateTimeField()),
                ('tank', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='stations.tank')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='tenants.tenant')),
            ],
            options={
                'constraints': [models.UniqueConstraint(models.F('tank'), condition=models.Q(('deleted_at__isnull', True)), name='tankstatus_one_per_tank')],
            },
        ),
        migrations.CreateModel(
            name='ShiftSummary',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('day', models.DateField()),
                ('litres', models.DecimalField(decimal_places=3, max_digits=14)),
                ('flagged', models.BooleanField(default=False)),
                ('cash', models.JSONField(default=dict)),
                ('computed_at', models.DateTimeField()),
                ('shift', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='operations.shift')),
                ('station', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='stations.station')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='tenants.tenant')),
            ],
            options={
                'indexes': [models.Index(fields=['station', 'day'], name='shiftsummary_station_day')],
                'constraints': [models.UniqueConstraint(models.F('shift'), condition=models.Q(('deleted_at__isnull', True)), name='shiftsummary_one_per_shift')],
            },
        ),
        enable_rls("operations_dailysalesrollup", tenant_isolation()),
        enable_rls("operations_tankstatus", tenant_isolation()),
        enable_rls("operations_shiftsummary", tenant_isolation()),
    ]
