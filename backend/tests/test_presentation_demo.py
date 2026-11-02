"""The presentation company (demo_profile "presentation"): 8 stations, built through the real services, showing smooth
days, the problems PumpWise catches, and approvals in action."""
import time
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.core.db import db_context
from apps.onboarding import presentation
from apps.onboarding.demo import data_for, seed_demo_tenant
from apps.operations.approvals import end_summary
from apps.operations.models import DayClose, ShiftAttendant, UndoRequest
from apps.reports.pumps import pump_report
from apps.reports.reconciliation import reconcile_tank_day
from apps.stations.models import Station, Tank
from apps.tenants.models import Tenant


def test_the_presentation_company_shows_smooth_days_problems_caught_and_approvals():
    began = time.monotonic()
    info = seed_demo_tenant(name="Savanna Test", days=3, password="Private-demo-pass-9", profile="presentation")
    print(f"loaded 3 days of 8 stations in {time.monotonic() - began:.0f} s")
    login = APIClient().post("/api/auth/login/", {"identifier": info["logins"]["owner"], "password": info["password"]}, format="json")
    owner = APIClient()
    owner.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
    assert len(owner.get("/api/stations/").json()["results"]) == 8
    yesterday = timezone.localdate() - timedelta(days=1)

    with db_context(platform_admin=True):
        tenant = Tenant.objects.get(pk=info["tenant_id"])
    assert tenant.demo_profile == "presentation" and data_for(tenant)["profile"] == "presentation"  # the nightly reload keeps it
    with db_context(tenant_id=tenant.id):
        stations = {s.name: s for s in Station.objects.all()}
        # every station closed yesterday, a little after 21:00
        closes = DayClose.objects.filter(day=yesterday)
        assert closes.count() == 8 and all(timezone.localtime(c.closed_at).hour == 21 for c in closes)

        # cash: whole dollars handed in, within $1, except the three shortages planted
        ends = [r for r in ShiftAttendant.objects.filter(approval="approved").select_related("user", "shift__station")]
        diffs = [Decimal(end_summary(r)["cash_difference"]) for r in ends]
        short = sorted(d for d in diffs if abs(d) >= 1)  # 41.00, 23.50 and 12.20 short, plus the cents of the whole dollars
        assert len(short) == 3 and -42 < short[0] < -40 and -25 < short[1] < -22.5 and -13.5 < short[2] < -11, short
        assert len(diffs) > 80
        # approvals are stamped when the manager did them, not when the demo was loaded
        row = ends[0]
        assert row.left_at < row.approval_at < row.left_at + timedelta(hours=2)

        # sent back and fixed; a change asked for, allowed and made
        log = {a.action: a for a in AuditLog.objects.filter(action__in=["shift_end_sent_back", "shift_end_fixed", "undo_requested", "undo_allowed"])}
        assert set(log) == {"shift_end_sent_back", "shift_end_fixed", "undo_requested", "undo_allowed"}
        assert "EcoCash" in log["shift_end_sent_back"].changes["note"]
        assert UndoRequest.objects.get().status == "used"
        assert not ShiftAttendant.objects.filter(approval="sent_back").exists()

        # a pump with nobody on it for a night
        nights = [s for st in pump_report([stations["Borrowdale Road"]], yesterday - timedelta(days=1), yesterday) for s in st["shifts"]]
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
    assert len(data["stations"]) == 8 and len(data["staff"]) == 36
