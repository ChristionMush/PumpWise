"""Phase 6 (dashboard) and Phase 7 (forecast, alerts, daily summary, settings)."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.accounts.models import Role
from apps.core.db import db_context
from apps.insights import alerts as alert_rules
from apps.insights.forecast import forecast_tank
from apps.insights.messaging import ConsoleProvider, MessageProvider
from apps.insights.models import Alert, OutboundMessage

from .helpers import client_for, dip, make_forecourt, make_tenant, make_user, run_shift, two_tenants


def seed_history(a, fc, litres_for_day, days=14):
    """One shift per past day (10:00-12:00 UTC); litres_for_day(date) -> litres through nozzle 1."""
    reading, now = Decimal(0), timezone.now()
    for ago in range(days, 0, -1):
        day = (now - timedelta(days=ago)).replace(hour=10, minute=0, second=0, microsecond=0)
        litres = Decimal(litres_for_day(timezone.localtime(day).date()))
        run_shift(a["tenant"], a[Role.ATTENDANT], fc, [reading, 0], [reading + litres, 0], started_at=day, ended_at=day + timedelta(hours=2))
        reading += litres
    return reading


def tank_forecast(a, fc):
    with db_context(tenant_id=a["tenant"].id):
        from apps.stations.models import Tank

        return forecast_tank(Tank.objects.select_related("fuel_type").get(pk=fc.tank.pk))


# ---- forecast -------------------------------------------------------------------------------------------------

def test_days_to_empty_is_available_litres_over_average_daily_usage():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    seed_history(a, fc, lambda d: 500, days=10)
    dip(a["tenant"], a[Role.OWNER], fc, 3500, timezone.now() - timedelta(minutes=1))  # reserve is 500 -> 3000 usable
    f = tank_forecast(a, fc)
    assert f["status"] == "ok" and f["avg_daily_litres"] == "500.0" and f["available_litres"] == "3000.0"
    assert f["days_to_empty_simple"] == 6.0 and f["days_to_empty"] == 6.0
    today = timezone.localdate()
    assert f["empty_date"] == (today + timedelta(days=6)).isoformat() and f["refill_by"] == (today + timedelta(days=4)).isoformat()  # 2 days' lead
    assert f["low"] is False


def test_weekday_weighting_follows_the_tanks_own_pattern():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    seed_history(a, fc, lambda d: 400 if d.weekday() == 5 else 200, days=28)  # Saturdays are double
    dip(a["tenant"], a[Role.OWNER], fc, 3100, timezone.now() - timedelta(minutes=1))
    f = tank_forecast(a, fc)
    assert 6.5 <= f["days_to_empty"] <= 13  # bounded by the busiest and quietest days
    assert f["days_to_empty"] != f["days_to_empty_simple"]


def test_a_new_tank_is_not_diluted_by_days_before_it_was_used_and_estimates_track_sales_since_the_dip():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    assert tank_forecast(a, fc)["status"] == "no_dip"
    seed_history(a, fc, lambda d: 100, days=3)
    dip(a["tenant"], a[Role.OWNER], fc, 2000, timezone.now() - timedelta(days=2, hours=12))
    f = tank_forecast(a, fc)
    assert f["avg_daily_litres"] == "100.0"  # 3 days of history, not 28
    assert Decimal(f["current_litres"]) < Decimal(2000)  # sales since the dip are subtracted


# ---- alerts ---------------------------------------------------------------------------------------------------

def evaluate(a):
    with db_context(tenant_id=a["tenant"].id):
        return alert_rules.evaluate_tenant(a["tenant"].id)


def open_alerts(a):
    with db_context(tenant_id=a["tenant"].id):
        return list(Alert.objects.filter(status="open").values_list("kind", "severity"))


def test_low_stock_alert_appears_then_clears_itself_after_a_delivery():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    seed_history(a, fc, lambda d: 500, days=10)
    dip(a["tenant"], a[Role.OWNER], fc, 1500, timezone.now() - timedelta(minutes=1))  # 1000 usable = 2 days
    assert evaluate(a)["created"] >= 1 and ("low_stock", "warning") in open_alerts(a)
    assert evaluate(a)["created"] == 0  # idempotent
    assert [k for k, _ in open_alerts(a)].count("low_stock") == 1
    dip(a["tenant"], a[Role.OWNER], fc, 9000, timezone.now())
    assert evaluate(a)["resolved"] >= 1 and "low_stock" not in [k for k, _ in open_alerts(a)]


def test_at_or_below_reserve_is_critical():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    dip(a["tenant"], a[Role.OWNER], fc, 400, timezone.now())
    evaluate(a)
    assert ("low_stock", "critical") in open_alerts(a)


def test_missed_readings_are_flagged():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    evaluate(a)
    assert ("missed_reading", "info") in open_alerts(a)  # never dipped
    from apps.operations import services

    with db_context(tenant_id=a["tenant"].id):
        services.start_shift(user=a[Role.ATTENDANT], station=fc.station, started_at=timezone.now() - timedelta(hours=20), readings=[{"nozzle": fc.nozzles[0], "reading": Decimal(0)}])
    evaluate(a)
    assert ("missed_reading", "warning") in open_alerts(a)  # a shift left open for 20 hours


def test_loss_and_cash_alerts_persist_until_a_person_resolves_them():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="2.00")
    now = timezone.now()
    ten = now.replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=1)
    dip(a["tenant"], a[Role.OWNER], fc, 6000, ten - timedelta(hours=1))
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=ten, ended_at=ten + timedelta(hours=2), cash=[("cash", "USD", 100)])  # 100 short
    dip(a["tenant"], a[Role.OWNER], fc, 5500, ten + timedelta(hours=4))  # 6000 - 100 sold - 5500 = 400 L unexplained
    evaluate(a)
    kinds = [k for k, _ in open_alerts(a)]
    assert "loss" in kinds and "cash" in kinds
    evaluate(a)
    assert [k for k, _ in open_alerts(a)].count("loss") == 1
    mgr = client_for(a[Role.MANAGER])
    rows = mgr.get("/api/alerts/").json()["results"]
    loss = [r for r in rows if r["kind"] == "loss"][0]
    assert mgr.post(f"/api/alerts/{loss['id']}/resolve/").status_code == 400  # how it was resolved is required
    assert mgr.post(f"/api/alerts/{loss['id']}/resolve/", {"reason": "meter_typo", "note": "Pump 1 end was 100, not 1000"}, format="json").json()["status"] == "resolved"
    assert "loss" not in [r["kind"] for r in mgr.get("/api/alerts/").json()["results"]]
    assert "loss" in [r["kind"] for r in mgr.get("/api/alerts/?status=resolved").json()["results"]]
    evaluate(a)  # the loss is still in that day's numbers: the manager's answer stands, no new or reopened alert
    assert "loss" not in [k for k, _ in open_alerts(a)]
    with db_context(tenant_id=a["tenant"].id):
        assert Alert.objects.filter(kind="loss").count() == 1 and Alert.objects.get(kind="loss").reopened_count == 0


def test_alert_api_scoping_and_roles():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"])
    dip(a["tenant"], a[Role.OWNER], fc, 100, timezone.now())
    client_for(a[Role.OWNER]).post("/api/alerts/refresh/")
    alert_id = client_for(a[Role.OWNER]).get("/api/alerts/").json()["results"][0]["id"]
    assert client_for(a[Role.ATTENDANT]).get("/api/alerts/").status_code == 403
    assert client_for(b[Role.OWNER]).get("/api/alerts/").json()["count"] == 0
    assert client_for(b[Role.OWNER]).post(f"/api/alerts/{alert_id}/resolve/").status_code == 404


def test_background_alert_job_covers_every_tenant_and_one_failure_does_not_stop_the_rest(monkeypatch):
    a, b = two_tenants()
    for t in (a, b):
        dip(t["tenant"], t[Role.OWNER], make_forecourt(t["tenant"]), 100, timezone.now())
    from apps.insights import tasks

    real = tasks.evaluate_tenant

    def flaky(tid):
        if str(tid) == str(a["tenant"].id):
            raise RuntimeError("boom")
        return real(tid)

    monkeypatch.setattr(tasks, "evaluate_tenant", flaky)
    result = tasks.run_alerts.delay().get()
    assert result == {"ok": 1, "failed": 1}
    with db_context(tenant_id=b["tenant"].id):
        assert Alert.objects.count() >= 1


# ---- dashboard ------------------------------------------------------------------------------------------------

def test_dashboard_gives_the_owner_today_levels_refill_date_loss_and_shift_status():
    a, b = two_tenants()
    fc = make_forecourt(a["tenant"], price="2.00")
    seed_history(a, fc, lambda d: 500, days=10)
    now = timezone.now()
    dip(a["tenant"], a[Role.OWNER], fc, 3500, now - timedelta(minutes=10))
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [5000, 0], [5100, 0], started_at=now - timedelta(minutes=8), ended_at=now - timedelta(minutes=2), cash=[("cash", "USD", 200)])
    from apps.operations import services

    with db_context(tenant_id=a["tenant"].id):
        services.start_shift(user=a[Role.ATTENDANT], station=fc.station, started_at=now - timedelta(minutes=1), readings=[{"nozzle": fc.nozzles[0], "reading": Decimal(5100)}])
    d = client_for(a[Role.OWNER]).get("/api/dashboard/").json()
    st = d["stations"][0]
    assert st["name"] == "Main Station" and st["today"]["totals"] if "totals" in st["today"] else True
    assert d["totals"]["today_litres"] == "100.000" and d["totals"]["today_amount_by_currency"] == {"USD": "200.00"}
    assert st["tanks"][0]["fuel_code"] == "PETROL" and st["tanks"][0]["level_pct"] is not None and st["tanks"][0]["days_to_empty"] is not None
    assert d["next_refill"]["tank"] == "Tank 1" and d["next_refill"]["refill_by"]
    assert d["totals"]["open_shifts"] == 1 and st["open_shifts"][0]["attendant"]
    assert "unexplained_loss_7d" in d["totals"] and "open_alerts" in d["totals"]
    other = client_for(b[Role.OWNER]).get("/api/dashboard/").json()
    assert other["stations"] == [] and other["totals"]["today_litres"] == "0"  # B sees none of A


def test_dashboard_is_cacheable_and_answers_not_modified_when_nothing_changed():
    a, _ = two_tenants()
    make_forecourt(a["tenant"])
    c = client_for(a[Role.OWNER])
    first = c.get("/api/dashboard/")
    assert first["Cache-Control"] == "private, max-age=30" and first["ETag"]
    again = c.get("/api/dashboard/", HTTP_IF_NONE_MATCH=first["ETag"])
    assert again.status_code == 304 and again.content == b""


def test_dashboard_roles_and_station_scope():
    a, _ = two_tenants()
    f1, f2 = make_forecourt(a["tenant"], name="North"), make_forecourt(a["tenant"], name="South", code="DIESEL")
    assert client_for(a[Role.ATTENDANT]).get("/api/dashboard/").status_code == 403
    client_for(a[Role.OWNER]).put(f"/api/users/{a[Role.MANAGER].id}/stations/", {"station_ids": [str(f1.station.pk)]}, format="json")
    names = {s["name"] for s in client_for(a[Role.MANAGER]).get("/api/dashboard/").json()["stations"]}
    assert names == {"North"}
    assert {s["name"] for s in client_for(a[Role.OWNER]).get("/api/dashboard/").json()["stations"]} == {"North", "South"}
    del f2


def test_one_tap_price_update_applies_from_the_chosen_time_to_new_shifts():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="1.00")
    owner = client_for(a[Role.OWNER])
    now = timezone.now()
    r = owner.post("/api/prices/", {"station": str(fc.station.pk), "fuel_type": str(fc.fuel.pk), "price_per_litre": "1.25", "effective_from": (now - timedelta(hours=1)).isoformat()}, format="json")
    assert r.status_code == 201
    s = run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=now - timedelta(minutes=30), ended_at=now)
    assert owner.get(f"/api/shifts/{s.pk}/").json()["summary"]["expected"] == "125.00"


# ---- settings, branding, summary ------------------------------------------------------------------------------

def test_settings_are_owner_editable_manager_readable_and_validated():
    a, _ = two_tenants()
    owner, mgr = client_for(a[Role.OWNER]), client_for(a[Role.MANAGER])
    assert mgr.get("/api/settings/").json()["loss_threshold_litres"] == "20.00"
    assert mgr.patch("/api/settings/", {"low_stock_days": 5}, format="json").status_code == 403
    assert client_for(a[Role.ATTENDANT]).get("/api/settings/").status_code == 403
    r = owner.patch("/api/settings/", {"loss_threshold_litres": "10", "low_stock_days": 5, "summary_phone": "0772000111", "summary_channel": "whatsapp", "display_name": "Msasa Group"}, format="json")
    assert r.status_code == 200 and r.json()["summary_phone"] == "+263772000111" and r.json()["low_stock_days"] == 5
    assert owner.patch("/api/settings/", {"summary_phone": "xyz"}, format="json").status_code == 400
    assert owner.patch("/api/settings/", {"summary_channel": "pigeon"}, format="json").status_code == 400
    assert owner.get("/api/auth/me/").json()["branding"]["display_name"] == "Msasa Group"
    assert owner.get("/api/auth/me/").json()["branding"]["built_by"] == "Tendeka & Elichi"


def test_tenant_logo_upload_is_private_and_shows_in_branding():
    a, b = two_tenants()
    owner = client_for(a[Role.OWNER])
    png = SimpleUploadedFile("logo.png", b"\x89PNG\r\n\x1a\nx", content_type="image/png")
    r = owner.post("/api/settings/logo/", {"logo": png}, format="multipart")
    assert r.status_code == 200 and r.json()["logo_url"]
    url = owner.get("/api/auth/me/").json()["branding"]["logo_url"]
    assert owner.get(url).status_code == 200 and client_for(b[Role.OWNER]).get(url).status_code == 404
    assert client_for(a[Role.MANAGER]).post("/api/settings/logo/", {"logo": png}, format="multipart").status_code == 403


def test_daily_summary_text_and_sending_through_the_provider():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="2.00")
    now = timezone.now()
    dip(a["tenant"], a[Role.OWNER], fc, 100, now - timedelta(minutes=5))  # below reserve: needs attention
    run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [50, 0], started_at=now - timedelta(minutes=4), ended_at=now - timedelta(minutes=1), cash=[("cash", "USD", 100)])
    owner = client_for(a[Role.OWNER])
    owner.patch("/api/settings/", {"summary_phone": "0772000111", "summary_channel": "sms"}, format="json")
    text = owner.get("/api/summary/").json()["text"]
    assert "Sold 50 L / USD 100.00" in text and "Main Station: 50 L" in text and "LOW Main Station Tank 1" in text and "Needs attention" in text
    ConsoleProvider.sent.clear()
    res = owner.post("/api/summary/").json()
    assert res["status"] == "sent" and res["to"] == "+263772000111"
    assert ConsoleProvider.sent[0]["channel"] == "sms" and "Sold 50 L" in ConsoleProvider.sent[0]["body"]
    assert owner.get("/api/messages/").json()["results"][0]["kind"] == "daily_summary"
    assert client_for(a[Role.MANAGER]).post("/api/summary/").status_code == 403


def test_a_quiet_day_says_all_good_and_channel_off_is_skipped():
    a, _ = two_tenants()
    owner = client_for(a[Role.OWNER])
    assert "All good." in owner.get("/api/summary/").json()["text"]
    owner.patch("/api/settings/", {"summary_channel": "none"}, format="json")
    assert owner.post("/api/summary/").json()["status"] == "skipped"


def test_a_failing_provider_is_recorded_and_never_breaks_the_job(settings):
    a, _ = two_tenants()
    settings.MESSAGE_PROVIDER = "tests.test_insights.ExplodingProvider"
    owner = client_for(a[Role.OWNER])
    owner.patch("/api/settings/", {"summary_phone": "0772000111"}, format="json")
    res = owner.post("/api/summary/")
    assert res.status_code == 200 and res.json()["status"] == "failed" and "gateway down" in res.json()["detail"]


class ExplodingProvider(MessageProvider):
    def send(self, **kw):
        from apps.insights.messaging import MessageError

        raise MessageError("gateway down")


def test_daily_summary_job_sends_for_each_active_tenant_with_a_phone():
    from apps.insights import tasks

    a, b = two_tenants()
    suspended = make_tenant("Suspended Co")
    make_user(suspended, Role.OWNER)
    for t in (a, b):
        client_for(t[Role.OWNER]).patch("/api/settings/", {"summary_phone": "0772000111"}, format="json")
    with db_context(platform_admin=True):
        from apps.tenants.models import Tenant

        Tenant.objects.filter(pk=suspended.pk).update(status="suspended")
    ConsoleProvider.sent.clear()
    assert tasks.send_daily_summaries.delay().get() == {"ok": 2, "failed": 0}
    assert len(ConsoleProvider.sent) == 2
    with db_context(tenant_id=a["tenant"].id):
        assert OutboundMessage.objects.count() == 1


@pytest.mark.parametrize("path", ["/api/dashboard/", "/api/forecast/", "/api/alerts/", "/api/summary/", "/api/messages/", "/api/settings/"])
def test_insight_endpoints_require_sign_in(path):
    from rest_framework.test import APIClient

    assert APIClient().get(path).status_code == 401


def test_a_loss_and_the_gain_after_it_cancel_out_and_the_loss_alert_clears():
    """A dip taken while pumps were in use: fuel sold before it is counted after it. The next window shows the matching
    gain; together they balance, so the loss alert clears by itself and no gain alert is raised."""
    from apps.operations import services

    from .helpers import approve_ends

    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="2.00")
    att = a[Role.ATTENDANT]
    day = timezone.localtime().replace(hour=8, minute=0, second=0, microsecond=0) - timedelta(days=2)
    dip(a["tenant"], a[Role.OWNER], fc, 10000, day)
    with db_context(tenant_id=a["tenant"].id):
        shift = services.start_shift(user=att, station=fc.station, started_at=day + timedelta(hours=1), readings=[{"nozzle": n, "reading": Decimal(0)} for n in fc.nozzles])
    dip(a["tenant"], a[Role.OWNER], fc, 9900, day + timedelta(hours=12))  # 20:00, pumps in use: 100 L gone, meters not read yet
    evaluate(a)
    assert ("loss", "critical") in open_alerts(a)
    with db_context(tenant_id=a["tenant"].id):
        services.end_shift(user=att, shift=shift, ended_at=day + timedelta(hours=23), readings=[{"nozzle": fc.nozzles[0], "reading": Decimal(150)}, {"nozzle": fc.nozzles[1], "reading": Decimal(0)}],
                           cash=[{"payment_type": "cash", "currency": "USD", "amount": Decimal("300")}])
        approve_ends(att)
    dip(a["tenant"], a[Role.OWNER], fc, 9850, day + timedelta(hours=24))  # next morning, pumps stopped: 150 L sold since the first dip
    evaluate(a)
    assert "loss" not in [k for k, _ in open_alerts(a)]  # neither the loss nor the matching gain is open
    with db_context(tenant_id=a["tenant"].id):
        cleared = Alert.objects.get(kind="loss")
        assert cleared.status == "auto_resolved" and "Cancelled out" in cleared.resolution_note
    # the Fuel check report keeps the running difference per tank: the loss and the gain net to nothing
    period = {"date_from": (day - timedelta(days=1)).date().isoformat(), "date_to": timezone.localdate().isoformat()}
    totals = client_for(a[Role.MANAGER]).get("/api/reconciliation/tanks/", period).json()["totals"]
    assert [(t["unexplained"], t["sold"], t["status"], t["checks"]) for t in totals] == [("0.000", "150.000", "ok", 2)]


def test_a_cash_difference_that_is_corrected_clears_by_itself():
    from apps.operations import services

    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"], price="2.00")
    ten = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0) - timedelta(days=1)
    shift = run_shift(a["tenant"], a[Role.ATTENDANT], fc, [0, 0], [100, 0], started_at=ten, ended_at=ten + timedelta(hours=2), cash=[("cash", "USD", 100)])
    evaluate(a)
    assert "cash" in [k for k, _ in open_alerts(a)]  # 200 expected, 100 handed in
    with db_context(tenant_id=a["tenant"].id):  # the manager corrects a mistyped amount
        services.submit_cash(user=a[Role.MANAGER], shift=shift, who=a[Role.ATTENDANT], cash=[{"payment_type": "cash", "currency": "USD", "amount": Decimal("200")}])
    evaluate(a)
    assert "cash" not in [k for k, _ in open_alerts(a)]
    with db_context(tenant_id=a["tenant"].id):
        cleared = Alert.objects.get(kind="cash")
        assert cleared.status == "auto_resolved" and "corrected" in cleared.resolution_note
