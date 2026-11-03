"""The presentation company (demo_profile "presentation"): 2 stations, built through the real services, showing smooth
days, the problems PumpWise catches, and approvals in action."""
import time
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.onboarding import presentation
from apps.onboarding.demo import data_for, seed_demo_tenant, turn_into_presentation
from apps.operations.approvals import end_summary
from apps.operations.models import DayClose, ShiftAttendant, UndoRequest
from apps.reports.pumps import pump_report
from apps.reports.reconciliation import reconcile_tank_day
from apps.stations.models import Station, Tank
from apps.tenants.features import set_tenant_features
from apps.tenants.models import Tenant

from .helpers import PASSWORD, at, make_forecourt, make_user, run_shift, two_tenants


def test_the_presentation_company_shows_smooth_days_problems_caught_and_approvals():
    began = time.monotonic()
    info = seed_demo_tenant(name="Savanna Test", days=3, password="Private-demo-pass-9", profile="presentation")
    print(f"loaded 3 days of 2 stations in {time.monotonic() - began:.0f} s")
    login = APIClient().post("/api/auth/login/", {"identifier": info["logins"]["owner"], "password": info["password"]}, format="json")
    owner = APIClient()
    owner.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
    assert len(owner.get("/api/stations/").json()["results"]) == 2
    yesterday = timezone.localdate() - timedelta(days=1)

    with db_context(platform_admin=True):
        tenant = Tenant.objects.get(pk=info["tenant_id"])
    assert tenant.demo_profile == "presentation" and data_for(tenant)["profile"] == "presentation"  # the nightly reload keeps it
    with db_context(tenant_id=tenant.id):
        stations = {s.name: s for s in Station.objects.all()}
        # every station closed yesterday, a little after 21:00
        closes = DayClose.objects.filter(day=yesterday)
        assert closes.count() == 2 and all(timezone.localtime(c.closed_at).hour == 21 for c in closes)

        # cash: whole dollars handed in, within $1, except the three shortages planted
        ends = [r for r in ShiftAttendant.objects.filter(approval="approved").select_related("user", "shift__station")]
        diffs = [Decimal(end_summary(r)["cash_difference"]) for r in ends]
        short = sorted(d for d in diffs if abs(d) >= 1)  # 41.00, 23.50 and 12.20 short, plus the cents of the whole dollars
        assert len(short) == 3 and -42 < short[0] < -40 and -25 < short[1] < -22.5 and -13.5 < short[2] < -11, short
        assert len(diffs) > 12
        # approvals are stamped when the manager did them, not when the demo was loaded
        row = ends[0]
        assert row.cash_submitted_at < row.approval_at < row.cash_submitted_at + timedelta(hours=2)

        # sent back and fixed; a change asked for, allowed and made
        log = {a.action: a for a in AuditLog.objects.filter(action__in=["shift_end_sent_back", "shift_end_fixed", "undo_requested", "undo_allowed"])}
        assert set(log) == {"shift_end_sent_back", "shift_end_fixed", "undo_requested", "undo_allowed"}
        assert "EcoCash" in log["shift_end_sent_back"].changes["note"]
        assert UndoRequest.objects.get().status == "used"
        assert not ShiftAttendant.objects.filter(approval="sent_back").exists()

        # a pump with nobody on it for a night
        nights = [s for st in pump_report([stations["Samora Machel Avenue"]], yesterday - timedelta(days=1), yesterday) for s in st["shifts"]]
        assert any(s["missing"] == ["Pump 4"] for s in nights), nights

        # the fuel loss shows in the fuel check on the day it was planted, and only there
        checks = {(t.station.name, t.name): reconcile_tank_day(t, yesterday - timedelta(days=1)) for t in Tank.objects.select_related("station", "fuel_type")}
        lost = {k: Decimal(r["loss"]) for k, r in checks.items() if r["status"] != "insufficient_data" and Decimal(r["loss"]) > 100}
        assert list(lost) == [("Bulawayo CBD", "Petrol tank")] and 280 < lost[("Bulawayo CBD", "Petrol tank")] < 340, lost

    # the manager sees their own station only
    mgr = APIClient()
    mgr.credentials(HTTP_AUTHORIZATION="Bearer " + APIClient().post("/api/auth/login/", {"identifier": info["logins"]["sam-mgr"], "password": info["password"]}, format="json").json()["access"])
    assert [s["name"] for s in mgr.get("/api/stations/").json()["results"]] == ["Samora Machel Avenue"]


def test_the_presentation_data_is_made_up_to_now():
    now = timezone.now()
    data = presentation.generate(now=now, days=5)
    stamps = [e["at"] for e in data["events"]]
    assert stamps == sorted(stamps) and stamps[-1] < timezone.localtime(now).strftime("%Y-%m-%dT%H:%M")
    assert len(data["stations"]) == 2 and len(data["staff"]) == 8


def test_an_existing_business_becomes_the_litrix_presentation_with_its_own_people_and_logins():
    a, b = two_tenants()
    tenant = a["tenant"]
    with db_context(platform_admin=True):
        set_tenant_features(tenant=tenant, multi_attendant=True, pump_assignment=True)
    rose = make_user(tenant, Role.MANAGER, full_name="Rose", phone="+263770000101")
    stewart = make_user(tenant, Role.ATTENDANT, full_name="Stewart", phone="+263770000102")
    thabani = make_user(tenant, Role.ATTENDANT, full_name="Thabani", phone="+263770000103")
    fc = make_forecourt(tenant)  # its old test records
    run_shift(tenant, a[Role.ATTENDANT], fc, [0, 0], [10, 0], started_at=at(2026, 10, 1, 5), ended_at=at(2026, 10, 1, 15), cash=[("cash", "USD", 15)])
    other = make_forecourt(b["tenant"], name="Not Litrix")
    with db_context(tenant_id=tenant.id):
        people = set(User.objects.values_list("pk", flat=True))

    turn_into_presentation(tenant.id, "litrix", days=3)

    with db_context(platform_admin=True):
        tenant.refresh_from_db()
    assert tenant.is_demo and tenant.demo_profile == "litrix" and data_for(tenant)["company"] == "litrix"  # the nightly reload keeps it
    with db_context(tenant_id=tenant.id):
        assert set(User.objects.values_list("pk", flat=True)) == people  # nobody new
        assert [s.name for s in Station.objects.all()] == ["Cowdray Park Station"]
        assert set(ShiftAttendant.objects.values_list("user_id", flat=True)) == {stewart.pk, thabani.pk}  # the old test shift is gone
        assert AuditLog.objects.filter(action="shift_end_approved", actor=rose).exists()
        assert AuditLog.objects.filter(action="shift_end_sent_back").exists() and UndoRequest.objects.get().status == "used"
    with db_context(tenant_id=b["tenant"].id):
        assert Station.objects.filter(pk=other.station.pk).exists()  # another business is never touched
    # their logins still work
    login = APIClient().post("/api/auth/login/", {"identifier": "+263770000101", "password": PASSWORD}, format="json")
    assert login.status_code == 200, login.content
