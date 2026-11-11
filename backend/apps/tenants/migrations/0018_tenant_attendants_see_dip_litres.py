from django.db import migrations, models


class Migration(migrations.Migration):
    """A per-business switch, off everywhere: attendants who dip at the end of a shift see the litres from the dip chart."""

    dependencies = [
        ('tenants', '0017_remove_pooled_tanks'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='attendants_see_dip_litres',
            field=models.BooleanField(default=False),
        ),
    ]
