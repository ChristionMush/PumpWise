"""Phase 11: onboarding checklist and the demo business."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.onboarding.demo import seed_demo_tenant
from apps.stations.models import Pump

from .helpers import at, client_for, dip, make_forecourt, platform_client, run_shift, second_tank, two_tenants


def items(client):
    return {i["key"]: i for i in client.get("/api/onboarding/checklist/").json()["items"]}


def test_checklist_starts_empty_and_each_step_flips_when_the_real_work_is_done():
    a, _ = two_tenants()
    owner = client_for(a[Role.OWNER])
    first = owner.get("/api/onboarding/checklist/").json()
    assert first["done"] == 1 and first["percent"] == 11 and first["ready"] is False and first["total"] == 9  # only "attendant exists" (fixture) is done
    assert "qr" not in items(owner)  # QR labels are switched off: pumps and tanks are labelled physically
    fc = make_forecourt(a["tenant"], price=None)
    i = items(owner)
    assert i["stations"]["done"] and i["tanks"]["done"] and i["calibration"]["done"]
    assert i["pumps"]["done"]  # every nozzle on one tank is fine
    diesel = second_tank(fc)
    owner.put(f"/api/tanks/{diesel.pk}/calibration/", {"points": [{"dip_mm": 0, "litres": 0}, {"dip_mm": 1000, "litres": 10000}]}, format="json")
    assert owner.patch(f"/api/nozzles/{fc.nozzles[1].pk}/", {"tank": str(diesel.pk)}, format="json").status_code == 200
    assert items(owner)["pumps"]["done"]
    assert not items(owner)["prices"]["done"] and "PETROL" in items(owner)["prices"]["detail"]
    for fuel in (fc.fuel.pk, diesel.fuel_type_id):
        owner.post("/api/prices/", {"station": str(fc.station.pk), "fuel_type": str(fuel), "price_per_litre": "1.5"}, format="json")
    assert items(owner)["prices"]["done"]
    assert items(owner)["staff"]["done"]  # the fixture tenant already has an attendant
    assert not items(owner)["dips"]["done"] and "Tank 1" in items(owner)["dips"]["detail"]
    dip(a["tenant"], a[Role.OWNER], fc, 5000, timezone.now())
    owner.post("/api/tank-readings/", {"tank": str(diesel.pk), "litres": "4000"}, format="json")
    assert items(owner)["dips"]["done"]
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [10, 0], started_at=at(2026, 10, 1, 6), ended_at=at(2026, 10, 1, 12))
    assert items(owner)["shift"]["done"]
    assert not items(owner)["summary"]["done"]
    owner.patch("/api/settings/", {"summary_phone": "0772000111"}, format="json")
    res = owner.get("/api/onboarding/checklist/").json()
    assert res["ready"] is True and res["percent"] == 100


def test_calibration_and_nozzles_are_checked():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    owner = client_for(a[Role.OWNER])
    owner.post("/api/tanks/", {"station": str(fc.station.pk), "name": "Tank 2", "fuel_type": str(fc.fuel.pk), "capacity_litres": "1000"}, format="json")
    i = items(owner)
    assert not i["calibration"]["done"] and "Tank 2" in i["calibration"]["detail"]
    assert i["pumps"]["done"]  # one tank behind every nozzle is fine
    with db_context(tenant_id=a["tenant"].id):
        Pump.objects.create(tenant_id=a["tenant"].id, station=fc.station, name="Pump 2")  # no nozzle yet
    i = items(owner)
    assert not i["pumps"]["done"] and i["pumps"]["detail"] == "Needs a nozzle: Pump 2"


def test_checklist_roles_flags_and_tenant_separation():
    a, b = two_tenants()
    assert client_for(a[Role.ATTENDANT]).get("/api/onboarding/checklist/").status_code == 403
    assert client_for(a[Role.MANAGER]).get("/api/onboarding/checklist/").status_code == 200
    assert client_for(a[Role.OWNER]).post("/api/onboarding/flags/", {"key": "hack", "value": True}, format="json").status_code == 400
    assert client_for(a[Role.OWNER]).post("/api/onboarding/flags/", {"key": "qr_printed"}, format="json").status_code == 400  # QR step removed
    assert "qr" not in items(client_for(b[Role.OWNER]))


def test_the_setup_technician_can_follow_any_businesss_progress_without_seeing_their_data():
    a, b = two_tenants()
    make_forecourt(a["tenant"])
    p = platform_client()
    res = p.get(f"/api/platform/tenants/{a['tenant'].id}/onboarding/").json()
    assert {i["key"]: i["done"] for i in res["items"]}["stations"] is True
    assert {i["key"]: i["done"] for i in p.get(f"/api/platform/tenants/{b['tenant'].id}/onboarding/").json()["items"]}["stations"] is False
    assert client_for(a[Role.OWNER]).get(f"/api/platform/tenants/{a['tenant'].id}/onboarding/").status_code == 403
    assert p.get("/api/platform/tenants/not-a-uuid/onboarding/").status_code == 404


def test_the_demo_business_is_complete_realistic_and_shows_live_alerts():
    info = seed_demo_tenant(name="Demo Sales Co", days=14, password="Private-demo-pass-9")
    login = APIClient().post("/api/auth/login/", {"identifier": info["logins"]["owner"], "password": info["password"]}, format="json")
    assert login.status_code == 200 and login.json()["user"]["must_change_password"] is False
    owner = APIClient()
    owner.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
    # everything a sales walk-through needs
    assert len(owner.get("/api/stations/").json()["results"]) == 2 and owner.get("/api/pumps/").json()["count"] == 6
    sales = owner.get("/api/reports/daily-sales/?date_from=" + str(timezone.localdate() - timedelta(days=13))).json()
    assert Decimal(sales["totals"]["litres"]) > 10000 and sales["totals"]["amount_by_currency"]["USD"]
    d = owner.get("/api/dashboard/").json()
    assert d["totals"]["open_shifts"] == 1 and d["next_refill"] is not None
    # the demo's last finished shift ended an hour ago: it is "today" unless the trading day began (21:00) since then
    from apps.operations import tradingday

    if timezone.now() - timedelta(minutes=60) > tradingday.bounds(None, tradingday.today())[0]:
        assert Decimal(d["totals"]["today_litres"]) > 0
    assert d["totals"]["open_alerts"] >= 1 and Decimal(d["totals"]["unexplained_loss_7d"]) > 300 and d["totals"]["flagged_cash_shifts_7d"] >= 1
    kinds = {a["kind"] for a in owner.get("/api/alerts/").json()["results"]}
    assert "cash" in kinds and "loss" not in kinds  # the planted fuel loss is tracked in the Fuel check, not alerted
    # the planted loss is found where it was planted, and ONLY there
    yesterday = str(timezone.localdate() - timedelta(days=1))
    rows = owner.get(f"/api/reconciliation/tanks/?date_from={timezone.localdate() - timedelta(days=12)}&date_to={yesterday}").json()["rows"]
    flagged = [r for r in rows if r["flagged"]]
    assert [(r["date"], r["tank"]) for r in flagged] == [(yesterday, "Petrol tank")] and 360 < Decimal(flagged[0]["loss"]) < 400
    cash = owner.get(f"/api/reconciliation/cash/?date_from={timezone.localdate() - timedelta(days=12)}&flagged=1").json()["rows"]
    assert len(cash) == 1 and cash[0]["status"] == "short" and Decimal(cash[0]["variance"]) < -30
    # multi-currency and payment types are in there
    shift_id = owner.get("/api/shifts/?status=closed").json()["results"][0]["id"]
    kinds = {(c["payment_type"], c["currency"]) for c in owner.get(f"/api/shifts/{shift_id}/").json()["summary"]["by_payment_type"]}
    assert {("cash", "USD"), ("cash", "ZWG"), ("mobile_money", "USD"), ("card", "USD"), ("credit", "USD")} <= kinds
    assert owner.get("/api/onboarding/checklist/").json()["ready"] is True
    assert owner.get("/api/auth/me/").json()["billing"] is None  # paid up for a year: no nagging in a demo
    mgr = APIClient()
    mgr.credentials(HTTP_AUTHORIZATION="Bearer " + APIClient().post("/api/auth/login/", {"identifier": info["logins"]["manager"], "password": info["password"]}, format="json").json()["access"])
    assert mgr.get("/api/dashboard/").status_code == 200


def test_the_demo_is_deterministic_for_a_seed_and_the_command_demands_a_private_password_outside_dev(settings):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    a = seed_demo_tenant(name="Same A", days=5, seed=11, password="Private-demo-pass-9")
    b = seed_demo_tenant(name="Same B", days=5, seed=11, password="Private-demo-pass-9")

    def total(info):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer " + APIClient().post("/api/auth/login/", {"identifier": info["logins"]["owner"], "password": info["password"]}, format="json").json()["access"])
        return c.get("/api/reports/daily-sales/?date_from=" + str(timezone.localdate() - timedelta(days=5))).json()["totals"]["litres"]

    assert total(a) == total(b)
    settings.DEBUG = False
    with __import__("pytest").raises(CommandError):
        call_command("seed_demo")
