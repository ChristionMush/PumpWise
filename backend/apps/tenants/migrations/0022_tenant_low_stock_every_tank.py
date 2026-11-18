from django.db import migrations, models


class Migration(migrations.Migration):
    """A per-business switch (module "low_stock_every_tank"), off everywhere: low stock is raised only when every tank
    of a fuel at a station is low."""

    dependencies = [
        ('tenants', '0021_mark_2_starts_before_approval'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='low_stock_every_tank',
            field=models.BooleanField(default=False),
        ),
    ]
