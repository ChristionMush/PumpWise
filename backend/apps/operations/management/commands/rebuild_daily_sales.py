"""Rebuild every station's daily sales totals on the trading day (21:00 to 21:00, named by the closing day).

    python manage.py rebuild_daily_sales

Run once after moving to trading days; safe to run again (each day is recomputed from the sales themselves).
"""
from datetime import timedelta

from django.core.management.base import BaseCommand

from apps.core.tenancy import for_each_tenant


def rebuild(tenant_id):
    from apps.operations.models import DailySalesRollup, Transaction
    from apps.operations.rollups import refresh_daily
    from apps.operations.tradingday import Calendar
    from apps.stations.models import Station

    for station in Station.all_objects.all():
        cal = Calendar(station.pk)
        moments = list(Transaction.all_objects.filter(station=station).values_list("occurred_at", flat=True))
        days = {cal.day_of(m) for m in moments} | set(DailySalesRollup.objects.filter(station=station).values_list("day", flat=True))
        if not days:
            continue
        day = min(days)
        while day <= max(days):
            refresh_daily(station, day)
            day += timedelta(days=1)


class Command(BaseCommand):
    help = "Rebuild the daily sales totals on trading days (21:00 to 21:00)."

    def handle(self, *args, **opts):
        self.stdout.write(str(for_each_tenant(rebuild)))
