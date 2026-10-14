"""Phase 12: demo businesses, the nightly reload, and trial tenants."""
import json
from datetime import date, timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.core.db import db_context
from apps.insights.messaging import ConsoleProvider, send_message
from apps.insights.models import OutboundMessage
from apps.onboarding import demo
from apps.onboarding.tasks import refresh_demo_tenants, run_trial_lifecycle
from apps.operations.models import PumpMeterReading, Shift, TankReading, Transaction
from apps.stations.models import Station
from apps.tenants.models import Subscription, Tenant
from mock_data.generate_mock_data import generate as make_mock_data

from .helpers import at, client_for, dip, make_forecourt, platform_client, run_shift, two_tenants

PW = "Private-demo-pass-9"


@pytest.fixture
def small_demo(monkeypatch):
    """Trials and the nightly job load 14-30 days in production; 2 days is enough to test them."""
    real = demo.generate
    monkeypatch.setattr(demo, "generate", lambda **kw: real(**{**kw, "days": 2}))


def login(identifier, password):
    res = APIClient().post("/api/auth/login/", {"identifier": identifier, "password": password}, format="json")
    assert res.status_code == 200, res.content
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {res.json()['access']}")
    return c


def counts(tenant_id):
    with db_context(tenant_id=tenant_id):
        return (Shift.objects.count(), PumpMeterReading.objects.count(), Transaction.objects.count(), TankReading.objects.count(), Station.objects.count())


def real_business():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00")
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [10, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 14))
    dip(a["tenant"], a[Role.MANAGER], fc, 5000, at(2026, 10, 1, 15))
    return a


# ---- the mock data -----------------------------------------------------------------------------------------------

def test_the_generator_is_deterministic_and_ends_on_the_requested_day():
    one = make_mock_data(end=date(2026, 10, 13), days=10, seed=3)
    two = make_mock_data(end=date(2026, 10, 13), days=10, seed=3)
    assert json.dumps(one, default=str) == json.dumps(two, default=str)
    assert max(e["at"] for e in one["events"] if "at" in e).startswith("2026-10-13")
    assert min(e["at"] for e in one["events"] if "at" in e).startswith("2026-10-04")
    assert make_mock_data(end=date(2026, 10, 13), days=10, seed=4) != one


def test_the_committed_seed_file_matches_the_generator_format():
    data = demo.load_file()
    assert data["version"] == 1 and {s["key"] for s in data["staff"]} == {"manager", "att1", "att2"}
    assert {s["attendants_mode"] for s in data["stations"]} == {"single", "multiple"}


# ---- seed_demo -------------------------------------------------------------------------------------------------------

def test_seed_demo_loads_the_file_through_the_orm_and_is_idempotent(tmp_path, settings):
    settings.DEBUG = True
    path = tmp_path / "seed.json"
    demo.write_file(demo.generate(days=4, seed=5, name="Seeded Demo"), path)
    call_command("seed_demo", file=str(path), password=PW)
    with db_context(platform_admin=True):
        tenant = Tenant.objects.get(name="Seeded Demo")
    assert tenant.is_demo
    first = counts(tenant.id)
    assert first[0] > 0 and first[4] == 2
    call_command("seed_demo", file=str(path))  # again: wiped and reloaded, never duplicated
    assert counts(tenant.id) == first
    with db_context(platform_admin=True):
        assert Tenant.objects.filter(name="Seeded Demo").count() == 1


def test_seed_demo_refuses_a_real_business_with_the_same_name(tmp_path):
    a = real_business()
    path = tmp_path / "seed.json"
    demo.write_file(demo.generate(days=2, name=a["tenant"].name), path)
    before = counts(a["tenant"].id)
    with pytest.raises(Exception, match="real business"):
        call_command("seed_demo", file=str(path), password=PW)
    assert counts(a["tenant"].id) == before


def test_wiping_refuses_any_business_that_is_not_a_demo():
    a = real_business()
    before = counts(a["tenant"].id)
    with pytest.raises(demo.NotDemoTenant):
        demo.wipe_demo_data(a["tenant"].id)
    with pytest.raises(demo.NotDemoTenant):
        demo.reload_demo_tenant(a["tenant"].id, demo.generate(days=2))
    assert counts(a["tenant"].id) == before


# ---- the nightly reload ----------------------------------------------------------------------------------------------

def test_the_nightly_job_reloads_demos_up_to_yesterday_and_never_touches_a_real_business(small_demo, tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "DEMO_FILE", tmp_path / "seed_demo.json")
    a = real_business()
    real_before = counts(a["tenant"].id)
    info = demo.seed_demo_tenant(name="Nightly Demo", days=3, seed=1, password=PW)
    with db_context(tenant_id=info["tenant_id"]):
        Shift.objects.update(notes="changed by a prospect")
    result = refresh_demo_tenants()
    assert result == {"reloaded": 1, "failed": 0}
    assert counts(a["tenant"].id) == real_before  # the real business is untouched
    with db_context(tenant_id=a["tenant"].id):
        assert not Shift.objects.filter(notes="changed by a prospect").exists()
    with db_context(tenant_id=info["tenant_id"]):
        assert not Shift.objects.filter(notes="changed by a prospect").exists()  # demo data was reloaded fresh
        newest = max(timezone.localtime(s.started_at).date() for s in Shift.objects.filter(status="closed"))
        assert newest >= timezone.localdate() - timedelta(days=1)
    login(info["logins"]["owner"], PW)  # the same logins keep working after the reload
    assert json.loads((tmp_path / "seed_demo.json").read_text())["end"] == str(timezone.localdate() - timedelta(days=1))


# ---- demo businesses never message anyone -----------------------------------------------------------------------------

def test_a_demo_business_never_sends_real_messages():
    info = demo.seed_demo_tenant(name="Quiet Demo", days=2, seed=2, password=PW)
    ConsoleProvider.sent.clear()
    with db_context(tenant_id=info["tenant_id"]):
        msg = send_message(tenant_id=info["tenant_id"], channel="sms", to="+263770000001", kind="daily_summary", body="hi")
        assert msg.status == "skipped" and "demo" in msg.detail
        assert not OutboundMessage.objects.filter(status="sent").exists()
    assert ConsoleProvider.sent == []
    real = real_business()
    with db_context(tenant_id=real["tenant"].id):
        assert send_message(tenant_id=real["tenant"].id, channel="sms", to="+263770000002", kind="daily_summary", body="hi").status == "sent"


def test_me_tells_the_app_to_show_the_demo_banner():
    info = demo.seed_demo_tenant(name="Banner Demo", days=2, seed=2, password=PW)
    owner = login(info["logins"]["owner"], PW)
    assert owner.get("/api/auth/me/").json()["tenant"]["is_demo"] is True
    a, _ = two_tenants()
    assert client_for(a[Role.OWNER]).get("/api/auth/me/").json()["tenant"]["is_demo"] is False


# ---- trials -------------------------------------------------------------------------------------------------------------

def test_platform_creates_a_trial_with_a_one_time_password_and_a_14_day_expiry(small_demo):
    res = platform_client().post("/api/platform/trials/", {"prospect_name": "Mbare Fuels"}, format="json")
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["is_demo"] is True and body["trial_expires_at"] == str(timezone.localdate() + timedelta(days=14))
    assert res["Cache-Control"] == "no-store" and body["owner_credentials"]["temporary_password"]
    assert len(body["stations"]) == 2
    owner = login(body["owner_credentials"]["login"], body["owner_credentials"]["temporary_password"])
    owner.post("/api/auth/change-password/", {"current_password": body["owner_credentials"]["temporary_password"], "new_password": "Prospect-pass-77"}, format="json")
    owner = login(body["owner_credentials"]["login"], "Prospect-pass-77")
    assert owner.get("/api/dashboard/").json()["stations"]  # filled from the template


def test_only_the_platform_can_create_trials():
    a, _ = two_tenants()
    for role in (Role.OWNER, Role.MANAGER, Role.ATTENDANT):
        assert client_for(a[role]).post("/api/platform/trials/", {"prospect_name": "Sneaky"}, format="json").status_code == 403


def _trial(name, expires):
    res = platform_client().post("/api/platform/trials/", {"prospect_name": name}, format="json").json()
    with db_context(platform_admin=True):
        Tenant.objects.filter(pk=res["id"]).update(trial_expires_at=expires)
    return res


def test_expired_trials_are_suspended_then_removed_and_converted_ones_are_kept(small_demo):
    today = timezone.localdate()
    fresh = _trial("Still Trying", today + timedelta(days=3))
    expired = _trial("Just Expired", today - timedelta(days=1))
    stale = _trial("Long Gone", today - timedelta(days=31))
    converted = _trial("Signed Up", today - timedelta(days=40))
    real = real_business()
    assert platform_client().post(f"/api/platform/tenants/{converted['id']}/convert-trial/", {}, format="json").json()["is_demo"] is False
    assert run_trial_lifecycle() == {"suspended": 1, "removed": 1}
    with db_context(platform_admin=True):
        status = {t.name: t.status for t in Tenant.all_objects.filter(pk__in=[fresh["id"], expired["id"], stale["id"], converted["id"]])}
        assert status == {"Still Trying": "active", "Just Expired": "suspended", "Long Gone": "cancelled", "Signed Up": "active"}
        assert Tenant.all_objects.get(pk=stale["id"]).deleted_at is not None
        assert Subscription.objects.get(tenant_id=expired["id"]).status == "suspended"
    assert counts(stale["id"])[0] == 0 and counts(converted["id"])[0] == 0  # demo data gone
    with db_context(tenant_id=stale["id"]):
        assert not User.objects.filter(is_active=True).exists()  # logins switched off
    assert counts(real["tenant"].id)[0] == 1  # a real business is never part of this


def test_converting_needs_a_trial():
    a, _ = two_tenants()
    assert platform_client().post(f"/api/platform/tenants/{a['tenant'].id}/convert-trial/", {}, format="json").status_code == 400
