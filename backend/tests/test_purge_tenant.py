"""purge_tenant: the manual, audited deletion of a test or demo business (docs/retention.md)."""
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.onboarding import demo
from apps.tenants.purge import NotDemoTenant, purge_tenant

from .test_demo_and_trials import real_business

PW = "Private-demo-pass-9"


def rows(tenant_id):
    return purge_tenant(tenant_id, allow_real=True, dry_run=True)["rows"]


def forced_tables():
    with connections["owner"].cursor() as cur:
        cur.execute("SELECT relname FROM pg_class WHERE relforcerowsecurity ORDER BY 1")
        return [t for (t,) in cur.fetchall()]


def can_login(identifier):
    return APIClient().post("/api/auth/login/", {"identifier": identifier, "password": PW}, format="json").status_code == 200


def test_purging_a_demo_removes_every_row_and_leaves_other_businesses_alone():
    real = real_business()
    real_before = rows(real["tenant"].id)
    info = demo.seed_demo_tenant(name="Purge Demo", days=2, password=PW)
    assert len(rows(info["tenant_id"])) > 5 and can_login(info["logins"]["owner"])
    forced = forced_tables()

    call_command("purge_tenant", info["slug"], confirm=info["slug"])

    with pytest.raises(LookupError):
        rows(info["tenant_id"])
    assert rows(real["tenant"].id) == real_before
    assert forced_tables() == forced  # Row-Level Security is back exactly as it was
    assert not can_login(info["logins"]["owner"])
    with db_context(platform_admin=True):
        entry = AuditLog.objects.get(action="tenant_purged", object_id=info["tenant_id"])
    assert entry.tenant_id is None and entry.changes["name"] == "Purge Demo"
    # start afresh: the same name can be used again
    again = demo.seed_demo_tenant(name="Purge Demo", days=2, password=PW)
    assert can_login(again["logins"]["owner"])


def test_without_confirm_it_is_a_dry_run(capsys):
    info = demo.seed_demo_tenant(name="Dry Demo", days=2, password=PW)
    before = rows(info["tenant_id"])
    call_command("purge_tenant", info["slug"])
    assert "DRY RUN" in capsys.readouterr().out
    assert rows(info["tenant_id"]) == before
    with pytest.raises(CommandError, match="repeat the slug"):
        call_command("purge_tenant", info["slug"], confirm="something-else")
    assert rows(info["tenant_id"]) == before


def test_a_real_business_needs_the_explicit_flag():
    real = real_business()
    slug = real["tenant"].slug
    before = rows(real["tenant"].id)
    with pytest.raises(NotDemoTenant):
        purge_tenant(real["tenant"].id, dry_run=False)
    with pytest.raises(CommandError, match="real business"):
        call_command("purge_tenant", slug, confirm=slug)
    assert rows(real["tenant"].id) == before
    call_command("purge_tenant", slug, confirm=slug, real_business=True)
    with pytest.raises(LookupError):
        rows(real["tenant"].id)


def test_listing_shows_demo_and_real_businesses(capsys):
    real_business()
    demo.seed_demo_tenant(name="Listed Demo", days=2, password=PW)
    call_command("purge_tenant")
    out = capsys.readouterr().out
    assert "REAL" in out and "demo" in out and "Listed Demo" in out
