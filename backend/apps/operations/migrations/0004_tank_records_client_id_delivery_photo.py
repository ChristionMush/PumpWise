import apps.stations.models
from django.db import migrations, models


class Migration(migrations.Migration):
    """Rule 11: dips and deliveries carry a client-made UUID (idempotent retries) and a delivery may have a note photo."""

    dependencies = [
        ('operations', '0003_rls'),
        ('tenants', '0006_tenantsettings_onboarding_flags'),
    ]

    operations = [
        migrations.AddField(
            model_name='tankreading',
            name='client_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='delivery',
            name='client_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='delivery',
            name='photo',
            field=models.FileField(blank=True, null=True, upload_to=apps.stations.models.upload_to),
        ),
        migrations.AddConstraint(
            model_name='tankreading',
            constraint=models.UniqueConstraint(models.F('tenant'), models.F('client_id'), condition=models.Q(('client_id__isnull', False)), name='tankreading_client_id_unique'),
        ),
        migrations.AddConstraint(
            model_name='delivery',
            constraint=models.UniqueConstraint(models.F('tenant'), models.F('client_id'), condition=models.Q(('client_id__isnull', False)), name='delivery_client_id_unique'),
        ),
    ]
