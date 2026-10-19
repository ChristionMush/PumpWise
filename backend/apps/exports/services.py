"""Build a tenant's export: every record they own, as JSON and one CSV per table, in a single zip.

Runs inside the tenant's database context, so Row-Level Security guarantees only their rows are read.
The exportable tables are an explicit allowlist (not introspection): a new table is either added here on purpose
or listed in EXCLUDED, and tests/test_exports.py fails the build until a developer decides.
"""
import csv
import io
import json
import zipfile
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from django.apps import apps as django_apps
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

# (app_label, model_name)
EXPORT_MODELS = [
    ("tenants", "tenant"), ("tenants", "tenantsettings"), ("tenants", "subscription"), ("accounts", "user"),
    ("stations", "station"), ("stations", "stationassignment"), ("stations", "fueltype"), ("stations", "tank"), ("stations", "calibrationpoint"),
    ("stations", "pump"), ("stations", "nozzle"), ("stations", "fuelprice"), ("stations", "exchangerate"),
    ("operations", "shift"), ("operations", "shiftattendant"), ("operations", "pumpassignment"), ("operations", "pumpmeterreading"), ("operations", "tankreading"), ("operations", "delivery"),
    ("operations", "cashsubmission"), ("operations", "transaction"),
    ("insights", "alert"), ("insights", "alertevent"), ("insights", "outboundmessage"), ("billing", "payment"), ("audit", "auditlog"),
]
# Tables with tenant data that are deliberately not exported (technical bookkeeping), each with the reason.
EXCLUDED = {
    "operations_syncoperation": "offline-sync idempotency log; its content is already in the records it created",
    "billing_billingreminder": "internal reminder bookkeeping",
    "billing_paymentevent": "platform-only webhook log",
    "exports_tenantexport": "the export requests themselves",
    "operations_dailysalesrollup": "dashboard rollup, derived from the exported transactions",
    "operations_tankstatus": "dashboard rollup, derived from the exported dips, deliveries and transactions",
    "operations_shiftsummary": "dashboard rollup, derived from the exported shifts, cash and transactions",
}
SECRET_FIELDS = {"password", "token_version"}


def _plain(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime,)):
        return timezone.localtime(value).isoformat() if timezone.is_aware(value) else value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if hasattr(value, "name") and hasattr(value, "storage"):  # a FieldFile
        return value.name or None
    return str(value)


def _rows(model):
    manager = getattr(model, "all_objects", None) or model._default_manager  # include soft-deleted rows: it is their data
    fields = [f for f in model._meta.concrete_fields if f.name not in SECRET_FIELDS]
    cols = [f.attname for f in fields]
    out = []
    for obj in manager.order_by("pk").iterator():
        row = {}
        for f in fields:
            row[f.attname] = _plain(getattr(obj, f.attname) if not isinstance(f, models.FileField) else getattr(obj, f.attname))
        out.append(row)
    return cols, out


def build_zip(tenant) -> tuple[bytes, dict]:
    tables, counts = {}, {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for app_label, name in EXPORT_MODELS:
            model = django_apps.get_model(app_label, name)
            cols, rows = _rows(model)
            table = model._meta.db_table
            tables[table], counts[table] = rows, len(rows)
            out = io.StringIO()
            w = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else v)) for k, v in r.items()})
            z.writestr(f"csv/{table}.csv", out.getvalue())
        z.writestr("data.json", json.dumps({"exported_at": timezone.now(), "business": tenant.name, "tables": tables}, cls=DjangoJSONEncoder, indent=1))
        z.writestr("README.txt", f"All data of {tenant.name} held by PumpWise (built by Tendeka & Elichi).\n"
                                 "data.json has everything; csv/ has one spreadsheet-ready file per table.\n"
                                 "Deleted records are included and have a deleted_at date. Password hashes are never exported.\n")
    return buf.getvalue(), counts
