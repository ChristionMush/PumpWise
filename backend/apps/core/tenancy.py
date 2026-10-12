"""Run background work tenant by tenant, each inside its own database context."""
import logging

from .db import db_context

log = logging.getLogger(__name__)


def tenant_ids(*, only_active=True):
    from apps.tenants.models import Tenant

    with db_context(platform_admin=True):
        qs = Tenant.objects.all()
        if only_active:
            qs = qs.filter(status="active")
        return list(qs.values_list("id", flat=True))


def for_each_tenant(fn, *, only_active=True):
    """Call fn(tenant_id) inside that tenant's context. One tenant failing never stops the others."""
    done, failed = 0, 0
    for tid in tenant_ids(only_active=only_active):
        try:
            with db_context(tenant_id=tid):
                fn(tid)
            done += 1
        except Exception:  # noqa: BLE001
            failed += 1
            log.exception("background job failed for tenant %s", tid)
    return {"ok": done, "failed": failed}
