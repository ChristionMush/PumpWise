"""Phase 8: billing, webhooks, reminders, auto-suspension, reactivation."""
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.billing import services
from apps.billing.models import BillingReminder, Payment, PaymentEvent, PricingPlan, Reseller
from apps.billing.providers import sign
from apps.core.db import db_context
from apps.insights.messaging import ConsoleProvider
from apps.insights.models import OutboundMessage
from apps.stations.models import FuelType, Station
from apps.tenants.models import Subscription, Tenant

from .helpers import client_for, make_forecourt, platform_client, two_tenants

SECRET = "test-webhook-secret"
TODAY = date(2026, 10, 15)


def post_event(body, secret=SECRET, provider="hmac", ts=None, tamper=False):
    raw = json.dumps(body).encode()
    headers = sign(secret, raw, ts)
    if tamper:
        raw = raw.replace(b"payment.succeeded", b"payment.succeedeD")
    return APIClient().post(f"/api/billing/webhook/{provider}/", data=raw, content_type="application/json", HTTP_X_TIMESTAMP=headers["X-Timestamp"], HTTP_X_SIGNATURE=headers["X-Signature"])


def paid(event_id, tenant, amount="30.00", months=1, type="payment.succeeded"):
    return {"id": event_id, "type": type, "amount": amount, "currency": "USD", "months": months, "tenant_id": str(tenant.id)}


def new_customer(event_id="evt_new_1", **over):
    c = {"business_name": "Chitungwiza Fuel", "owner_name": "Farai Dube", "owner_phone": "0773111222", "stations": ["Seke Road"], "plan": "standard", **over}
    return {"id": event_id, "type": "payment.succeeded", "amount": "80.00", "currency": "USD", "months": 1, "customer": c}


def set_sub(tenant, **fields):
    with db_context(platform_admin=True):
        Subscription.objects.filter(tenant_id=tenant.id).update(**fields)


def sub_of(tenant):
    with db_context(platform_admin=True):
        return Subscription.objects.get(tenant_id=tenant.id)


def tenant_status(tenant):
    with db_context(platform_admin=True):
        return Tenant.objects.get(pk=tenant.id).status


def cycle(today):
    return services.run_billing_cycle(today=today)


def make_plan(**over):
    with db_context(platform_admin=True):
        return PricingPlan.objects.create(code="standard", name="Standard", setup_fee_per_station=Decimal("50"), monthly_fee_per_station=Decimal("30"),
                                          multi_station_discounts=[{"min_stations": 3, "percent": 10}, {"min_stations": 6, "percent": 20}],
                                          addons={"whatsapp": {"name": "WhatsApp summary", "monthly_fee": "5.00"}}, **over)


# ---- pricing ----------------------------------------------------------------------------------------------------

def test_pricing_has_setup_fee_per_station_monthly_fee_multi_station_discount_and_addons():
    plan = make_plan()
    one = services.quote(plan, 1)
    assert (one["setup_fee"], one["monthly_stations"], one["monthly_total"], one["first_payment"]) == ("50.00", "30.00", "30.00", "80.00")
    three = services.quote(plan, 3, ["whatsapp"], months=2)
    assert three["discount_percent"] == 10 and three["monthly_stations"] == "81.00" and three["monthly_total"] == "86.00"
    assert three["setup_fee"] == "150.00" and three["first_payment"] == "322.00"  # 150 + 86 x 2
    assert services.quote(plan, 6)["discount_percent"] == 20
    with pytest.raises(ValueError):
        services.quote(plan, 1, ["nope"])


def test_platform_manages_plans_and_quotes_tenants_cannot():
    a, _ = two_tenants()
    p = platform_client()
    body = {"code": "pro", "name": "Pro", "monthly_fee_per_station": "45", "setup_fee_per_station": "60", "multi_station_discounts": [{"min_stations": 2, "percent": 5}], "addons": {"x": {"name": "X", "monthly_fee": "2"}}}
    assert p.post("/api/platform/billing/plans/", body, format="json").status_code == 201
    assert p.post("/api/platform/billing/plans/", {**body, "multi_station_discounts": [{"oops": 1}]}, format="json").status_code == 400
    q = p.post("/api/platform/billing/quote/", {"plan": "pro", "stations": 2, "addons": ["x"]}, format="json").json()
    assert q["monthly_total"] == "87.50" and q["setup_fee"] == "120.00"  # 45x2x0.95 + 2
    assert p.post("/api/platform/billing/quote/", {"plan": "ghost"}, format="json").status_code == 400
    assert p.post("/api/platform/billing/quote/", {"plan": "pro", "stations": 0}, format="json").status_code == 400
    for role in (Role.OWNER, Role.MANAGER, Role.ATTENDANT):
        assert client_for(a[role]).get("/api/platform/billing/plans/").status_code == 403
        assert client_for(a[role]).post("/api/platform/billing/quote/", {}, format="json").status_code == 403


# ---- webhook security -------------------------------------------------------------------------------------------

def test_webhook_rejects_bad_signatures_stale_timestamps_and_unknown_providers():
    a, _ = two_tenants()
    ev = paid("evt_1", a["tenant"])
    assert post_event(ev, secret="wrong-secret").status_code == 400
    assert post_event(ev, tamper=True).status_code == 400
    assert post_event(ev, ts=int(timezone.now().timestamp()) - 3600).status_code == 400  # replay of an old request
    assert post_event(ev, provider="stripe").status_code == 404
    assert APIClient().post("/api/billing/webhook/hmac/", data=b"{}", content_type="application/json").status_code == 400
    assert post_event({"id": "x", "type": "payment.succeeded"}).status_code == 400  # signed but malformed
    with db_context(platform_admin=True):
        assert PaymentEvent.objects.count() == 0 and Payment.objects.count() == 0
    # all failures look the same to the caller
    assert post_event(ev, secret="wrong").content == post_event(ev, tamper=True).content


def test_webhook_is_idempotent_a_repeated_event_is_applied_once():
    a, _ = two_tenants()
    set_sub(a["tenant"], paid_through=TODAY)
    first = post_event(paid("evt_dup", a["tenant"]))
    again = post_event(paid("evt_dup", a["tenant"]))
    assert first.json()["status"] in ("paid", "reactivated") and again.json() == {"status": "duplicate"}
    with db_context(platform_admin=True):
        assert Payment.objects.filter(tenant_id=a["tenant"].id).count() == 1
    assert sub_of(a["tenant"]).paid_through == services.add_months(TODAY, 1) if TODAY > timezone.localdate() else True


# ---- payments: extend, reactivate -------------------------------------------------------------------------------

def test_payment_extends_from_the_due_date_when_early_and_from_today_when_late():
    a, b = two_tenants()
    today = timezone.localdate()
    set_sub(a["tenant"], paid_through=today + timedelta(days=10))
    set_sub(b["tenant"], paid_through=today - timedelta(days=40))
    post_event(paid("evt_a", a["tenant"]))
    post_event(paid("evt_b", b["tenant"], months=3))
    assert sub_of(a["tenant"]).paid_through == services.add_months(today + timedelta(days=10), 1)  # no days lost by paying early
    assert sub_of(b["tenant"]).paid_through == services.add_months(today, 3)  # lapsed: counted from payment day
    assert sub_of(a["tenant"]).status == "active"


def test_month_arithmetic_handles_short_months_and_year_end():
    assert services.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert services.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert services.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_paying_reactivates_a_suspended_business_instantly_without_losing_anything():
    a, _ = two_tenants()
    fc = make_forecourt(a["tenant"])
    today = timezone.localdate()
    set_sub(a["tenant"], paid_through=today - timedelta(days=12))
    cycle(today)
    assert tenant_status(a["tenant"]) == "suspended"
    owner = client_for(a[Role.OWNER])
    assert owner.get("/api/stations/").status_code == 403
    r = post_event(paid("evt_react", a["tenant"]))
    assert r.json()["status"] == "reactivated"
    assert tenant_status(a["tenant"]) == "active" and sub_of(a["tenant"]).status == "active" and sub_of(a["tenant"]).suspended_at is None
    assert owner.get("/api/stations/").status_code == 200  # same token, no re-login
    with db_context(tenant_id=a["tenant"].id):
        assert Station.objects.filter(pk=fc.station.pk).exists()  # nothing was deleted


def test_a_cancelled_tenant_is_not_reactivated_by_a_payment():
    a, _ = two_tenants()
    with db_context(platform_admin=True):
        Tenant.objects.filter(pk=a["tenant"].id).update(status="cancelled")
    post_event(paid("evt_c", a["tenant"]))
    assert tenant_status(a["tenant"]) == "cancelled"
    with db_context(platform_admin=True):
        assert Payment.objects.filter(tenant_id=a["tenant"].id).count() == 1  # still recorded


def test_unknown_tenant_and_failed_payments_are_recorded_not_applied():
    a, _ = two_tenants()
    r = post_event({"id": "evt_u", "type": "payment.succeeded", "amount": "5", "currency": "USD", "tenant_id": "00000000-0000-4000-8000-000000000000"})
    assert r.status_code == 200 and r.json()["status"] == "unknown_tenant"
    before = sub_of(a["tenant"]).paid_through
    assert post_event(paid("evt_f", a["tenant"], type="payment.failed")).json()["status"] == "failed_recorded"
    assert sub_of(a["tenant"]).paid_through == before
    with db_context(platform_admin=True):
        assert Payment.objects.get(tenant_id=a["tenant"].id).status == "failed"


def test_reseller_share_is_configurable_and_totalled():
    a, _ = two_tenants()
    p = platform_client()
    rid = p.post("/api/platform/billing/resellers/", {"name": "Harare Partners", "revenue_share_pct": "20"}, format="json").json()["id"]
    assert p.post("/api/platform/billing/resellers/", {"name": "Bad", "revenue_share_pct": "120"}, format="json").status_code == 400
    p.patch(f"/api/platform/tenants/{a['tenant'].id}/subscription/", {"reseller": rid}, format="json")
    post_event(paid("evt_r1", a["tenant"], amount="50.00"))
    post_event(paid("evt_r2", a["tenant"], amount="30.00"))
    pays = p.get(f"/api/platform/billing/payments/?reseller={rid}").json()["results"]
    assert sorted(x["reseller_share"] for x in pays) == ["10.00", "6.00"]
    assert p.get("/api/platform/billing/resellers/").json()[0]["total_share"] == "16.00"
    assert client_for(a[Role.OWNER]).get("/api/platform/billing/resellers/").status_code == 403


# ---- new subscription auto-creates the business -------------------------------------------------------------------

def test_a_successful_payment_for_a_new_subscription_creates_tenant_owner_and_welcome_invite():
    ConsoleProvider.sent.clear()
    r = post_event(new_customer())
    assert r.status_code == 200 and r.json()["status"] == "tenant_created", r.content
    tid = r.json()["tenant_id"]
    with db_context(platform_admin=True):
        t = Tenant.objects.get(pk=tid)
        sub = Subscription.objects.get(tenant_id=tid)
        assert t.name == "Chitungwiza Fuel" and sub.status == "active" and sub.plan == "standard" and sub.paid_through
    with db_context(tenant_id=tid):
        assert [s.name for s in Station.objects.all()] == ["Seke Road"] and FuelType.objects.count() == 2
        owner = User.objects.get(role="owner")
        assert owner.must_change_password and owner.phone == "+263773111222"
        assert OutboundMessage.objects.get().kind == "welcome_invite"
    invite = ConsoleProvider.sent[0]
    assert invite["to"] == "+263773111222" and "Tendeka & Elichi" in invite["body"]
    temp = invite["body"].split("Temporary password: ")[1].splitlines()[0]  # on its own line: nothing stuck to it when copied
    login = APIClient().post("/api/auth/login/", {"identifier": "0773111222", "password": temp}, format="json")
    assert login.status_code == 200 and login.json()["user"]["must_change_password"] is True


def test_signup_payment_is_idempotent_too():
    first = post_event(new_customer("evt_same")).json()
    again = post_event(new_customer("evt_same")).json()
    assert first["status"] == "tenant_created" and again["status"] == "duplicate"
    with db_context(platform_admin=True):
        assert Tenant.objects.count() == 1 and Payment.objects.count() == 1


def test_signup_that_cannot_create_the_account_leaves_nothing_half_made_and_is_recorded_for_follow_up():
    a, _ = two_tenants()
    taken = a[Role.OWNER].email
    r = post_event(new_customer("evt_clash", owner_email=taken, owner_phone=None))
    assert r.status_code == 200 and r.json()["status"] == "signup_conflict"
    with db_context(platform_admin=True):
        assert Tenant.objects.count() == 2 and Payment.objects.count() == 0  # only the two fixture tenants
        assert PaymentEvent.objects.get(event_id="evt_clash").result == "signup_conflict"
    assert post_event(new_customer("evt_bad", stations="oops")).json()["status"] == "signup_conflict"


# ---- the daily cycle: reminders then suspension -------------------------------------------------------------------

def reminders(tenant):
    with db_context(tenant_id=tenant.id):
        return sorted(BillingReminder.objects.values_list("stage", flat=True))


def test_reminders_go_out_at_day_1_3_and_7_once_each_then_suspend_at_day_10():
    a, b = two_tenants()
    due = TODAY - timedelta(days=0)
    set_sub(a["tenant"], paid_through=due)
    client_for(a[Role.OWNER]).patch("/api/settings/", {"summary_phone": "0772000111"}, format="json")  # where reminders are sent
    ConsoleProvider.sent.clear()
    assert cycle(due) == {"reminders": 0, "suspended": 0}  # due today: not late yet
    for late, expect in ((1, [1]), (2, [1]), (3, [1, 3]), (6, [1, 3]), (7, [1, 3, 7]), (9, [1, 3, 7])):
        cycle(due + timedelta(days=late))
        assert reminders(a["tenant"]) == expect, late
        cycle(due + timedelta(days=late))  # repeating the same day never re-sends
        assert reminders(a["tenant"]) == expect, late
    assert sub_of(a["tenant"]).status == "past_due" and tenant_status(a["tenant"]) == "active"
    assert len([m for m in ConsoleProvider.sent if "Reminder" in m["body"]]) == 3
    assert cycle(due + timedelta(days=10)) == {"reminders": 0, "suspended": 1}
    assert tenant_status(a["tenant"]) == "suspended" and sub_of(a["tenant"]).status == "suspended" and sub_of(a["tenant"]).suspended_at
    assert cycle(due + timedelta(days=11)) == {"reminders": 0, "suspended": 0}  # already done
    with db_context(tenant_id=a["tenant"].id):
        assert OutboundMessage.objects.filter(kind="billing_suspended").count() == 1
    assert tenant_status(b["tenant"]) == "active"  # B has no due date: untouched


def test_after_an_outage_only_the_latest_missed_reminder_is_sent():
    a, _ = two_tenants()
    set_sub(a["tenant"], paid_through=TODAY)
    cycle(TODAY + timedelta(days=8))  # job did not run for 8 days
    assert reminders(a["tenant"]) == [7]


def test_suspension_locks_owner_and_manager_but_attendants_keep_recording():
    a, _ = two_tenants()
    set_sub(a["tenant"], paid_through=TODAY)
    cycle(TODAY + timedelta(days=10))
    for role in (Role.OWNER, Role.MANAGER):
        r = client_for(a[role]).get("/api/stations/")
        assert r.status_code == 403 and r.json()["code"] == "tenant_suspended"
        assert client_for(a[role]).get("/api/billing/status/").status_code == 200  # the pay screen still works
    assert client_for(a[Role.ATTENDANT]).get("/api/stations/").status_code == 200


def test_in_app_banner_escalates_with_days_overdue_and_clears_after_payment():
    a, _ = two_tenants()
    today = timezone.localdate()
    owner = client_for(a[Role.OWNER])
    assert owner.get("/api/auth/me/").json()["billing"] is None
    set_sub(a["tenant"], paid_through=today - timedelta(days=2))
    b = owner.get("/api/auth/me/").json()["billing"]
    assert b["level"] == "warning" and b["days_past_due"] == 2 and "8 days" in b["message"]
    set_sub(a["tenant"], paid_through=today - timedelta(days=8))
    assert owner.get("/api/auth/me/").json()["billing"]["level"] == "critical"
    post_event(paid("evt_ban", a["tenant"]))
    assert owner.get("/api/auth/me/").json()["billing"] is None
    assert client_for(a[Role.ATTENDANT]).get("/api/auth/me/").json()["billing"] is None  # attendants never see billing


def test_reminders_are_also_emailed_when_the_owner_has_an_email():
    a, _ = two_tenants()
    set_sub(a["tenant"], paid_through=TODAY)
    mail.outbox.clear()
    cycle(TODAY + timedelta(days=1))
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [a[Role.OWNER].email] and "was due" in mail.outbox[0].body


def test_cancelled_tenants_are_left_alone_by_the_cycle():
    a, _ = two_tenants()
    set_sub(a["tenant"], paid_through=TODAY)
    with db_context(platform_admin=True):
        Tenant.objects.filter(pk=a["tenant"].id).update(status="cancelled")
    assert cycle(TODAY + timedelta(days=20)) == {"reminders": 0, "suspended": 0}


def test_billing_job_and_manual_runbook_commands(capsys):
    from apps.billing import tasks

    a, _ = two_tenants()
    set_sub(a["tenant"], paid_through=timezone.localdate() - timedelta(days=11))
    assert tasks.run_billing_cycle.delay().get()["suspended"] == 1
    call_command("tenant_status", a["tenant"].slug, "reactivate")
    assert tenant_status(a["tenant"]) == "active"
    call_command("tenant_status", a["tenant"].slug, "suspend")
    assert tenant_status(a["tenant"]) == "suspended"
    with db_context(tenant_id=a["tenant"].id):
        from apps.audit.models import AuditLog

        assert AuditLog.objects.filter(action="tenant_status_manual").count() == 2
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("tenant_status", "no-such-tenant", "suspend")


# ---- checkout and visibility ----------------------------------------------------------------------------------------

def test_checkout_gives_the_owner_a_pay_link_even_while_suspended():
    a, _ = two_tenants()
    make_plan()
    make_forecourt(a["tenant"])
    owner = client_for(a[Role.OWNER])
    assert owner.post("/api/billing/checkout/").status_code == 400  # no plan assigned yet
    platform_client().patch(f"/api/platform/tenants/{a['tenant'].id}/subscription/", {"plan": "standard"}, format="json")
    set_sub(a["tenant"], paid_through=TODAY)
    cycle(TODAY + timedelta(days=12))
    r = owner.post("/api/billing/checkout/")
    assert r.status_code == 200 and r.json()["amount"] == "30.00" and a["tenant"].id.hex in r.json()["url"].replace("-", "")
    assert client_for(a[Role.MANAGER]).post("/api/billing/checkout/").status_code == 403  # only the owner pays


def test_tenants_see_only_their_own_payments_and_cannot_touch_billing_tables():
    a, b = two_tenants()
    post_event(paid("evt_pa", a["tenant"], amount="30.00"))
    post_event(paid("evt_pb", b["tenant"], amount="99.00"))
    seen = client_for(a[Role.OWNER]).get("/api/billing/status/").json()["payments"]
    assert [p["amount"] for p in seen] == ["30.00"]
    with db_context(tenant_id=a["tenant"].id):
        assert Payment.objects.count() == 1
        assert Reseller.objects.count() == 0 and PaymentEvent.objects.count() == 0  # internal tables are invisible
        with pytest.raises(DatabaseError), transaction.atomic():
            Payment.objects.create(tenant_id=a["tenant"].id, provider="x", event_id="forged", status="succeeded", amount=1, currency="USD")
        assert Subscription.objects.update(paid_through=TODAY + timedelta(days=999)) == 0  # cannot extend own access
    assert client_for(a[Role.ATTENDANT]).get("/api/billing/status/").status_code == 403
