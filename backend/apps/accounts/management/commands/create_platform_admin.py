import getpass
import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Role, User
from apps.accounts.utils import normalize_email, normalize_phone
from apps.core.db import db_context


class Command(BaseCommand):
    help = "Create a platform admin (Tendeka & Elichi staff). Password via PLATFORM_ADMIN_PASSWORD env or prompt."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--email")
        parser.add_argument("--phone")

    def handle(self, *args, **opts):
        email, phone = normalize_email(opts["email"]), normalize_phone(opts["phone"])
        if not email and not phone:
            raise CommandError("Provide --email and/or --phone")
        password = os.environ.get("PLATFORM_ADMIN_PASSWORD") or getpass.getpass("Password: ")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages))
        with db_context(platform_admin=True):
            user = User.objects.create_user(
                tenant_id=None, role=Role.PLATFORM_ADMIN, full_name=opts["name"], email=email, phone=phone, password=password
            )
        self.stdout.write(self.style.SUCCESS(f"Platform admin created: {user.id}"))
