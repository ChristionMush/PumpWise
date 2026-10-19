import uuid

import django.db.models.deletion
from django.db import migrations, models

from apps.core.rls import enable_rls, tenant_isolation

# Alerts closed by the old code without a person were closed by the system: they become auto_resolved. FORCE ROW
# LEVEL SECURITY binds the owner role that runs migrations, so it is lifted for this one update and put back.
RELABEL = [
    "SET CONSTRAINTS ALL IMMEDIATE",
    "ALTER TABLE insights_alert NO FORCE ROW LEVEL SECURITY",
    "UPDATE insights_alert SET status = 'auto_resolved' WHERE status = 'resolved' AND resolved_by_id IS NULL",
    "ALTER TABLE insights_alert FORCE ROW LEVEL SECURITY",
    "SET CONSTRAINTS ALL DEFERRED",
]
UNLABEL = [
    "SET CONSTRAINTS ALL IMMEDIATE",
    "ALTER TABLE insights_alert NO FORCE ROW LEVEL SECURITY",
    "UPDATE insights_alert SET status = 'resolved' WHERE status IN ('auto_resolved', 'awaiting_approval')",
    "ALTER TABLE insights_alert FORCE ROW LEVEL SECURITY",
    "SET CONSTRAINTS ALL DEFERRED",
]


class Migration(migrations.Migration):
    """Alert workflow: resolutions with a reason and note, owner review and approval of write-offs, reminders and
    escalation, reopening, repeat patterns, and an event history per alert."""

    dependencies = [
        ('accounts', '0002_user_must_change_password'),
        ('core', '0001_initial'),
        ('insights', '0002_rls'),
        ('tenants', '0009_tenant_pump_system'),
    ]

    operations = [
        migrations.RemoveConstraint(model_name='alert', name='alert_one_open_per_key'),
        migrations.AlterField(model_name='alert', name='status', field=models.CharField(default='open', max_length=20)),
        migrations.AddField(model_name='alert', name='resolution_reason', field=models.CharField(blank=True, default='', max_length=30)),
        migrations.AddField(model_name='alert', name='resolution_note', field=models.TextField(blank=True, default='')),
        migrations.AddField(model_name='alert', name='reviewed_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(
            model_name='alert', name='reviewed_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user'),
        ),
        migrations.AddField(model_name='alert', name='reminded_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='alert', name='escalated_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='alert', name='reopened_count', field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name='alert', name='pattern', field=models.CharField(blank=True, default='', max_length=200)),
        migrations.RunSQL(sql=RELABEL, reverse_sql=UNLABEL),
        migrations.AddConstraint(
            model_name='alert',
            constraint=models.UniqueConstraint(models.F('tenant'), models.F('dedupe_key'), condition=models.Q(('deleted_at__isnull', True), ('status__in', ['open', 'awaiting_approval'])), name='alert_one_open_per_key'),
        ),
        migrations.CreateModel(
            name='AlertEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('deleted_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('kind', models.CharField(max_length=30)),
                ('note', models.TextField(blank=True, default='')),
                ('data', models.JSONField(blank=True, default=dict)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounts.user')),
                ('alert', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='events', to='insights.alert')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='tenants.tenant')),
            ],
            options={'ordering': ['created_at']},
        ),
        enable_rls('insights_alertevent', tenant_isolation()),
    ]
