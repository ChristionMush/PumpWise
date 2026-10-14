from django.db import migrations, models


class Migration(migrations.Migration):
    """Demo and trial businesses (PLAN.md Phase 12)."""

    dependencies = [
        ('tenants', '0007_tenant_feature_grants'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='is_demo',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='tenant',
            name='trial_expires_at',
            field=models.DateField(blank=True, null=True),
        ),
    ]
