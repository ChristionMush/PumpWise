from django.db import migrations, models


class Migration(migrations.Migration):
    """A delivery's value (the supplier's invoice total, in the station's currency). Blank on every delivery recorded
    so far: their supplier, value and note number can be added later."""

    dependencies = [
        ("operations", "0018_remove_tank_switch"),
    ]

    operations = [
        migrations.AddField(
            model_name="delivery",
            name="value",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="delivery",
            name="currency",
            field=models.CharField(blank=True, default="", max_length=3),
        ),
        migrations.AddConstraint(
            model_name="delivery",
            constraint=models.CheckConstraint(condition=models.Q(("value__isnull", True), ("value__gt", 0), _connector="OR"), name="delivery_value_positive"),
        ),
    ]
