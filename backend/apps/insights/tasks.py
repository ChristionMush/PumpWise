from celery import shared_task
from django.utils import timezone

from apps.core.tenancy import for_each_tenant

from .alerts import evaluate_tenant
from .messaging import send_message
from .summary import build_daily_summary


@shared_task
def run_alerts():
    return for_each_tenant(lambda tid: evaluate_tenant(tid))


def send_summary_for(tenant_id):
    from apps.tenants.services import get_settings

    cfg = get_settings(tenant_id)
    return send_message(tenant_id=tenant_id, channel=cfg.summary_channel, to=cfg.summary_phone, kind="daily_summary", body=build_daily_summary(tenant_id, timezone.localdate()))


@shared_task
def send_daily_summaries():
    return for_each_tenant(send_summary_for)
