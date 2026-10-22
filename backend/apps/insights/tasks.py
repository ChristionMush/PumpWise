from celery import shared_task

from apps.core.tenancy import for_each_tenant

from .alerts import evaluate_tenant
from .messaging import send_message
from .summary import build_daily_summary


@shared_task
def run_alerts():
    """Hourly: check every rule, then remind managers of alerts open 12 hours and tell owners about those open 24."""
    from .alert_actions import escalate

    return for_each_tenant(lambda tid: {**evaluate_tenant(tid), **escalate(tid)})


@shared_task
def refresh_rollups():
    """Tank status rollups (level estimate, forecast) age with time even when nothing is recorded (rule 9), and open
    assigned-pump shifts get their sales so far."""
    from apps.operations.rollups import refresh_open_shifts, refresh_tenant_tanks

    def refresh(tenant_id):
        refresh_open_shifts(tenant_id)
        return refresh_tenant_tanks(tenant_id)

    return for_each_tenant(refresh)


def send_summary_for(tenant_id):
    from apps.tenants.services import get_settings

    cfg = get_settings(tenant_id)
    return send_message(tenant_id=tenant_id, channel=cfg.summary_channel, to=cfg.summary_phone, kind="daily_summary", body=build_daily_summary(tenant_id))  # the current trading day


@shared_task
def send_daily_summaries():
    return for_each_tenant(send_summary_for)
