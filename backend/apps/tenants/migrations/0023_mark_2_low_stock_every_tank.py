from django.db import migrations


def mark_2_on(apps, schema_editor):
    """Mark 2 (the PESNAT way) includes "low stock only when every tank is low". Only the Mark changes: each business
    keeps its own modules, so PESNAT gets it when it is ticked on its own page."""
    schema_editor.execute("SELECT set_config('app.platform_admin', 'on', true)")  # the marks table is platform only
    db = schema_editor.connection.alias
    Mark = apps.get_model("tenants", "Mark")
    for mark in Mark.objects.using(db).filter(name="Mark 2"):
        mark.modules = {**mark.modules, "low_stock_every_tank": True}
        mark.save(update_fields=["modules"])


def mark_2_off(apps, schema_editor):
    schema_editor.execute("SELECT set_config('app.platform_admin', 'on', true)")
    db = schema_editor.connection.alias
    Mark = apps.get_model("tenants", "Mark")
    for mark in Mark.objects.using(db).filter(name="Mark 2"):
        mark.modules = {k: v for k, v in mark.modules.items() if k != "low_stock_every_tank"}
        mark.save(update_fields=["modules"])


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0022_tenant_low_stock_every_tank"),
    ]

    operations = [migrations.RunPython(mark_2_on, mark_2_off)]
