from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.db import db_context
from apps.onboarding.demo import DEMO_FILE, generate, load_file, reload_demo_tenant, seed_demo_tenant, write_file
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = (
        "Load the demo business from mock_data/seed_demo.json through the real services (Row-Level Security applies). "
        "Idempotent: running it again wipes and reloads the same demo business. It only ever touches businesses marked "
        "is_demo; a real business with the same name is refused."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEMO_FILE), help="mock data file (default: mock_data/seed_demo.json)")
        parser.add_argument("--generate", dest="regenerate", action="store_true", help="regenerate the file first, with --end set to yesterday")
        parser.add_argument("--name", help="business name (default: the one in the file)")
        parser.add_argument("--password", help="Required outside development for a NEW demo business: its logins are real logins, so pick a private one.")

    def handle(self, *args, file, regenerate, name, password, **opts):
        if regenerate:
            data = generate()
            write_file(data, file)
            self.stdout.write(f"Regenerated {file} up to {data['end']}")
        else:
            try:
                data = load_file(file)
            except FileNotFoundError:
                raise CommandError(f"{file} not found. Run: python mock_data/generate_mock_data.py (or pass --generate).")
        name = name or data["business"]["name"]
        with db_context(platform_admin=True):
            existing = Tenant.objects.filter(name=name).first()
        if existing is not None:
            if not existing.is_demo:
                raise CommandError(f"'{name}' is a real business. The demo loader never touches real businesses.")
            reload_demo_tenant(existing.id, data)
            self.stdout.write(self.style.SUCCESS(f"Demo business reloaded: {name} ({existing.slug}); logins unchanged."))
            return
        if not password:
            if not settings.DEBUG:
                raise CommandError("Pass --password (demo accounts are real accounts; do not use a shared default).")
            password = "Demo-pass-2026"
        info = seed_demo_tenant(name=name, password=password, data=data)
        self.stdout.write(self.style.SUCCESS(f"Demo business ready: {name} ({info['slug']})"))
        for role, login in info["logins"].items():
            self.stdout.write(f"  {role:10} {login}")
        self.stdout.write(f"  password   {info['password']}")
