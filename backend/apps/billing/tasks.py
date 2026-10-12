from celery import shared_task

from .services import run_billing_cycle as _cycle


@shared_task
def run_billing_cycle():
    return _cycle()
