from django.db import migrations, models


class Migration(migrations.Migration):
    """A per-business switch (module "start_before_approval"), off everywhere: attendants may start their next shift
    while their last end of shift still waits for the manager."""

    dependencies = [
        ('tenants', '0019_mark'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='start_before_approval',
            field=models.BooleanField(default=False),
        ),
    ]
