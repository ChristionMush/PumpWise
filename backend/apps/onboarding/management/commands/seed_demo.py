from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.onboarding.demo import seed_demo_tenant


class Command(BaseCommand):
    help = "Create a demo business with ~30 days of realistic sample data (stations, tanks, pumps, sales, deliveries, one loss day, one cash shortage)."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Demo Fuel Services")
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--password", help="Required outside development: demo logins are real logins, so pick a private one.")

    def handle(self, *args, name, days, password, **opts):
        if not password:
            if not settings.DEBUG:
                raise CommandError("Pass --password (demo accounts are real accounts; do not use a shared default).")
            password = "Demo-pass-2026"
        info = seed_demo_tenant(name=name, days=days, password=password)
        self.stdout.write(self.style.SUCCESS(f"Demo business ready: {name} ({info['slug']})"))
        for role, login in info["logins"].items():
            self.stdout.write(f"  {role:9} {login}")
        self.stdout.write(f"  password  {info['password']}")
