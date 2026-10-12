from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.audit import services as audit
from apps.core.db import db_context
from apps.tenants.models import Tenant

from ...models import TenantExport
from ...storage import get_storage
from ...tasks import run_export


class Command(BaseCommand):
    help = "Support: export ONE tenant's data now and print a time-limited download link. Usage: export_tenant <slug>"

    def add_arguments(self, parser):
        parser.add_argument("slug")

    def handle(self, *args, slug, **opts):
        with db_context(platform_admin=True):
            tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            raise CommandError(f"No tenant with slug '{slug}'")
        with db_context(tenant_id=tenant.id):  # only this tenant's rows are visible while building
            export = TenantExport.objects.create(tenant_id=tenant.id, kind="manual")
            export = run_export(export.pk)
            url = get_storage().link(export, settings.EXPORT_LINK_TTL_SECONDS) if export.status == "ready" else None
        with db_context(tenant_id=tenant.id, platform_admin=True):  # platform flag ONLY for the audit row, never while reading data
            audit.log("export_by_support", tenant_id=tenant.id, obj=export, changes={"status": export.status}, via_platform=True)
        if export.status != "ready":
            raise CommandError(f"Export failed: {export.error}")
        self.stdout.write(self.style.SUCCESS(f"{slug}: {export.size_bytes} bytes, {sum(export.row_counts.values())} rows"))
        self.stdout.write(url)
