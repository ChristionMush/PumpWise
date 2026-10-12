from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.bootstrap import bootstrap


class Command(BaseCommand):
    help = "Create the app_owner / app_user roles and the database (idempotent). Needs superuser env vars."

    def add_arguments(self, parser):
        parser.add_argument("--recreate", action="store_true", help="DROP and recreate the database (DEV ONLY)")

    def handle(self, *args, **opts):
        if opts["recreate"] and not settings.DEBUG:
            raise CommandError("--recreate is only allowed with DJANGO_DEBUG=true")
        default, owner = settings.DATABASES["default"], settings.DATABASES["owner"]
        bootstrap(
            host=default["HOST"],
            port=default["PORT"],
            admin_user=settings.DB_ADMIN_USER,
            admin_password=settings.DB_ADMIN_PASSWORD,
            db_name=default["NAME"],
            owner=owner["USER"],
            owner_password=owner["PASSWORD"],
            app_user=default["USER"],
            app_password=default["PASSWORD"],
            recreate=opts["recreate"],
        )
        self.stdout.write(self.style.SUCCESS("Database roles and privileges are ready."))
