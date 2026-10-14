"""Permanently delete one business and everything in it (test and demo businesses; see docs/retention.md).

    python manage.py purge_tenant                                   # list every business
    python manage.py purge_tenant <slug>                            # dry run: what would be deleted
    python manage.py purge_tenant <slug> --confirm <slug>           # delete a demo or trial business
    python manage.py purge_tenant <slug> --confirm <slug> --real-business   # a business not marked demo

Take a backup first (deploy/backup.sh). This cannot be undone except by restoring that backup.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.tenants.purge import NotDemoTenant, find_tenant, list_tenants, purge_tenant


class Command(BaseCommand):
    help = "Permanently delete one business (dry run unless --confirm repeats its slug)."

    def add_arguments(self, parser):
        parser.add_argument("slug", nargs="?", help="the business's slug (or id); omit to list businesses")
        parser.add_argument("--confirm", help="type the slug again to really delete")
        parser.add_argument("--real-business", action="store_true", help="allow deleting a business that is not marked demo")

    def handle(self, *args, slug, confirm, real_business, **opts):
        if not slug:
            for t in list_tenants():
                kind = "trial" if t["trial_expires_at"] else ("demo" if t["is_demo"] else "REAL")
                self.stdout.write(f"{t['slug']:<32} {kind:<6} {t['status']:<10} {t['created_at']:%Y-%m-%d}  {t['name']}")
            return
        tenant = find_tenant(slug)
        if tenant is None:
            raise CommandError(f"No business with slug or id '{slug}'. Run without arguments to list them.")
        really = confirm is not None
        if really and confirm != tenant["slug"]:
            raise CommandError(f"--confirm must repeat the slug exactly: {tenant['slug']}")
        try:
            result = purge_tenant(tenant["id"], allow_real=real_business, dry_run=not really)
        except NotDemoTenant as e:
            raise CommandError(f"{e}. Nothing was deleted. Add --real-business if you are sure (export its data first).")

        for table, n in sorted(result["rows"].items()):
            self.stdout.write(f"  {table:<40} {n}")
        total = sum(result["rows"].values())
        if really:
            self.stdout.write(self.style.SUCCESS(f"Deleted {result['name']} ({result['slug']}): {total} rows. Recorded in the platform audit log."))
        else:
            self.stdout.write(f"DRY RUN: would delete {result['name']} ({result['slug']}), {total} rows. Nothing changed.")
            flag = "" if result["is_demo"] else " --real-business"
            self.stdout.write(f"To delete: python manage.py purge_tenant {result['slug']} --confirm {result['slug']}{flag}")
