from django.db import migrations


def mark_2_on(apps, schema_editor):
    """Mark 2 (the PESNAT way) includes "start before approval". Only the Mark changes: each business keeps its own
    modules, so PESNAT gets it when it is ticked on its own page."""
    schema_editor.execute("SELECT set_config('app.platform_admin', 'on', true)")  # the marks table is platform only
    db = schema_editor.connection.alias
    Mark = apps.get_model("tenants", "Mark")
    for mark in Mark.objects.using(db).filter(name="Mark 2"):
        mark.modules = {**mark.modules, "start_before_approval": True}
        mark.save(update_fields=["modules"])


def mark_2_off(apps, schema_editor):
    schema_editor.execute("SELECT set_config('app.platform_admin', 'on', true)")
    db = schema_editor.connection.alias
    Mark = apps.get_model("tenants", "Mark")
    for mark in Mark.objects.using(db).filter(name="Mark 2"):
        mark.modules = {k: v for k, v in mark.modules.items() if k != "start_before_approval"}
        mark.save(update_fields=["modules"])


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0020_tenant_start_before_approval"),
    ]

    operations = [migrations.RunPython(mark_2_on, mark_2_off)]
