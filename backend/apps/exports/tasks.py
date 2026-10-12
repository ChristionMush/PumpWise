import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.core.db import db_context
from apps.core.tenancy import for_each_tenant

from .models import TenantExport
from .services import build_zip
from .storage import get_storage

log = logging.getLogger(__name__)


def run_export(export_id):
    """Build the zip for one export. Call inside the tenant's context."""
    from apps.tenants.models import Tenant

    export = TenantExport.objects.get(pk=export_id)
    try:
        tenant = Tenant.objects.get(pk=export.tenant_id)
        data, counts = build_zip(tenant)
        now = timezone.now()
        key = f"{export.tenant_id}/{now:%Y-%m}/{export.pk}.zip"
        get_storage().save(key, data)
        export.file_key, export.size_bytes, export.row_counts = key, len(data), counts
        export.status = TenantExport.Status.READY
        export.expires_at = now + timedelta(days=settings.EXPORT_FILE_DAYS)
    except Exception as exc:  # noqa: BLE001
        log.exception("export %s failed", export_id)
        export.status, export.error = TenantExport.Status.FAILED, str(exc)[:300]
    export.save()
    return export


@shared_task
def build_export(tenant_id, export_id):
    with db_context(tenant_id=tenant_id):
        return str(run_export(export_id).status)


def _queue_monthly(tenant_id):
    export = TenantExport.objects.create(tenant_id=tenant_id, kind="monthly")
    run_export(export.pk)


@shared_task
def run_monthly_exports():
    """First of the month: a copy of every tenant's data into the versioned bucket (cancelled tenants too: their data is still held)."""
    from apps.core.tenancy import tenant_ids

    ids = tenant_ids(only_active=False)
    return for_each_tenant(_queue_monthly, only_active=False) | {"tenants": len(ids)}


@shared_task
def purge_expired_exports():
    """Delete expired export FILES (copies). The request records stay; business data is untouched."""
    n = 0

    def purge(tenant_id):
        nonlocal n
        for e in TenantExport.objects.filter(status="ready", kind="manual", expires_at__lt=timezone.now()):
            get_storage().delete(e.file_key)
            e.status = TenantExport.Status.EXPIRED
            e.save()
            n += 1

    for_each_tenant(purge, only_active=False)
    return n
