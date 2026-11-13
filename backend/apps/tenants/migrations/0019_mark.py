import uuid

import django.db.models.deletion
from django.db import migrations, models

from apps.core.rls import PLATFORM, Policy, enable_rls

# Frozen here (not imported from apps/tenants/modules.py), so this migration always does what it did when it ran.
MARK_1 = {"pump_allocation": "assigned", "expenses": False, "dip_at_shift_end": False, "attendants_see_dip_litres": False, "day_close": "manager"}
MARK_2 = {"pump_allocation": "pick", "expenses": True, "dip_at_shift_end": True, "attendants_see_dip_litres": True, "day_close": "18:00"}
MARKS = [
    ("Mark 1", "The Fuel Zone way: a site manager on the forecourt gives out the pumps, dips the tanks and closes the day from 21:00. "
               "Attendants sell and hand in their cash.", MARK_1),
    ("Mark 2", "The PESNAT way: no site manager on the forecourt. Attendants pick their pumps, dip the tanks at each handover and record "
               "what they pay out; the day closes at the evening handover. The owner reads the results remotely.", MARK_2),
]


def _modules(t):
    dip = t.dip_at_shift_end
    return {"pump_allocation": "pick" if t.attendants_pick_pumps and t.pump_assignment_enabled else t.pump_system,
            "expenses": t.expenses_enabled, "dip_at_shift_end": dip, "attendants_see_dip_litres": dip and t.attendants_see_dip_litres,
            "day_close": t.close_day_at_change or "manager"}


def seed(apps, schema_editor):
    """Mark 1 and Mark 2, and every business linked to the Mark its modules match most closely (Mark 1 on a tie):
    Fuel Zone to Mark 1, PESNAT and Litrix to Mark 2. Nothing a business does changes."""
    schema_editor.execute("SELECT set_config('app.platform_admin', 'on', true)")  # the marks table is platform only
    Mark, Tenant = apps.get_model("tenants", "Mark"), apps.get_model("tenants", "Tenant")
    marks = [Mark.objects.create(name=name, description=text, modules=values) for name, text, values in MARKS]
    for t in Tenant.objects.all():
        mods = _modules(t)
        best = min(marks, key=lambda m: sum(mods[k] != v for k, v in m.modules.items()))
        Tenant.objects.filter(pk=t.pk).update(mark=best)


class Migration(migrations.Migration):
    """Marks (docs/modules.md): named sets of modules a new business starts from. Platform only."""

    dependencies = [
        ("core", "0001_initial"),
        ("tenants", "0018_tenant_attendants_see_dip_litres"),
    ]

    operations = [
        migrations.CreateModel(
            name="Mark",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=80, unique=True)),
                ("description", models.TextField(blank=True, default="")),
                ("modules", models.JSONField(blank=True, default=dict)),
                ("based_on", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="tenants.mark")),
            ],
            options={"ordering": ["created_at"]},
        ),
        enable_rls("tenants_mark", [
            Policy("platform_select", "SELECT", using=PLATFORM),
            Policy("platform_insert", "INSERT", check=PLATFORM),
            Policy("platform_update", "UPDATE", using=PLATFORM, check=PLATFORM),
        ]),
        migrations.AddField(
            model_name="tenant",
            name="mark",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="tenants", to="tenants.mark"),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
