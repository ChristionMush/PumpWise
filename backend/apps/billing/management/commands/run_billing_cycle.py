from django.core.management.base import BaseCommand

from apps.billing.services import run_billing_cycle


class Command(BaseCommand):
    help = "Run the daily billing cycle now (reminders at day 1/3/7 past due, suspension at day 10). Safe to repeat."

    def handle(self, *args, **opts):
        self.stdout.write(str(run_billing_cycle()))
