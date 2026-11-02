import logging

from celery import shared_task

from apps.core.db import db_context
from apps.tenants.models import Tenant

from . import demo

log = logging.getLogger(__name__)


@shared_task
def refresh_demo_tenants(profile=None):
    """Nightly: regenerate the mock data up to yesterday and reload every active demo business, so demos always look live.
    Only businesses marked is_demo are ever touched (and wipe_demo_data re-checks that in the database).
    profile="presentation": only the presentation companies (after each shift change, so the shift working now is open)."""
    data = demo.generate()
    try:
        demo.write_file(data)
    except OSError:  # read-only image: the fresh data is still used below
        log.info("could not write %s; using the generated data in memory", demo.DEMO_FILE)
    with db_context(platform_admin=True):
        tenants = Tenant.objects.filter(is_demo=True, status=Tenant.Status.ACTIVE)
        tenants = list(tenants if profile is None else tenants.filter(demo_profile=profile))
    out = {"reloaded": 0, "failed": 0}
    for tenant in tenants:
        tid = tenant.id
        try:
            demo.reload_demo_tenant(tid, demo.data_for(tenant, data))  # the presentation company is made up to now
            out["reloaded"] += 1
        except Exception:  # noqa: BLE001 - one demo failing never stops the others
            out["failed"] += 1
            log.exception("demo reload failed for %s", tid)
    return out


@shared_task
def run_trial_lifecycle():
    return demo.run_trial_lifecycle()
