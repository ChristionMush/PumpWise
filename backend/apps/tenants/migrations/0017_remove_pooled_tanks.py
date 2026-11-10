from django.db import migrations


class Migration(migrations.Migration):
    """"Pumps draw from every tank of their fuel" goes: each nozzle now says which tanks it draws from (stations
    NozzleTank, linked for PESNAT's nozzles in stations 0008 before this field is removed)."""

    dependencies = [
        ('tenants', '0016_tenant_pooled_tanks'),
        ('stations', '0008_nozzle_also_draws_from'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='tenant',
            name='pooled_tanks',
        ),
    ]
