import logging

from celery import shared_task

from apps.core.db import db_context
from apps.tenants.models import Tenant

from . import demo

log = logging.getLogger(__name__)


@shared_task
def refresh_demo_tenants():
    """Nightly: regenerate the mock data up to yesterday and reload every active demo business, so demos always look live.
    Only businesses marked is_demo are ever touched (and wipe_demo_data re-checks that in the database)."""
    data = demo.generate()
    try:
        demo.write_file(data)
    except OSError:  # read-only image: the fresh data is still used below
        log.info("could not write %s; using the generated data in memory", demo.DEMO_FILE)
    with db_context(platform_admin=True):
        ids = list(Tenant.objects.filter(is_demo=True, status=Tenant.Status.ACTIVE).values_list("id", flat=True))
    out = {"reloaded": 0, "failed": 0}
    for tid in ids:
        try:
            demo.reload_demo_tenant(tid, data)
            out["reloaded"] += 1
        except Exception:  # noqa: BLE001 - one demo failing never stops the others
            out["failed"] += 1
            log.exception("demo reload failed for %s", tid)
    return out


@shared_task
def run_trial_lifecycle():
    return demo.run_trial_lifecycle()
