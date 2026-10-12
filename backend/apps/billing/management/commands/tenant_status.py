from django.core.management.base import BaseCommand, CommandError

from apps.audit import services as audit
from apps.core.db import db_context, tenant_scope
from apps.tenants.models import Tenant


class Command(BaseCommand):
    help = "Manually suspend / reactivate / cancel a tenant by slug (runbook). Nothing is deleted. Usage: tenant_status <slug> suspend|reactivate|cancel"

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("action", choices=["suspend", "reactivate", "cancel"])

    def handle(self, *args, slug, action, **opts):
        new = {"suspend": "suspended", "reactivate": "active", "cancel": "cancelled"}[action]
        with db_context(platform_admin=True):
            tenant = Tenant.objects.filter(slug=slug).first()
            if not tenant:
                raise CommandError(f"No tenant with slug '{slug}'")
            old, tenant.status = tenant.status, new
            tenant.save(update_fields=["status", "updated_at"])
            with tenant_scope(tenant.id):
                audit.log("tenant_status_manual", tenant_id=tenant.id, obj=tenant, changes={"status": [old, new]}, via_platform=True)
        self.stdout.write(self.style.SUCCESS(f"{slug}: {old} -> {new}"))
