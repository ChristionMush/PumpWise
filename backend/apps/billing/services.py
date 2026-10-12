"""Subscription lifecycle: payments extend access, silence suspends it, nothing is ever deleted."""
import calendar
import logging
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.core.db import tenant_scope
from apps.insights.messaging import send_message
from apps.tenants.models import Subscription, Tenant
from apps.tenants.services import create_tenant_with_owner, get_settings

from .models import BillingReminder, Payment, PaymentEvent, PricingPlan, Reseller

log = logging.getLogger(__name__)
Q2 = Decimal("0.01")


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


# ---- pricing ---------------------------------------------------------------------------------------------------

def quote(plan: PricingPlan, stations: int, addons=(), months=1):
    """Setup fee (once, per station) + monthly fee (per station, with the multi-station discount) + add-ons."""
    discount = max((t["percent"] for t in plan.multi_station_discounts if stations >= t["min_stations"]), default=0)
    stations_monthly = (plan.monthly_fee_per_station * stations * (Decimal(100) - Decimal(str(discount))) / 100).quantize(Q2)
    addon_lines = []
    for code in addons:
        a = plan.addons.get(code)
        if a is None:
            raise ValueError(f"Unknown add-on '{code}'")
        addon_lines.append({"code": code, "name": a["name"], "monthly_fee": str(Decimal(a["monthly_fee"]).quantize(Q2))})
    addons_monthly = sum((Decimal(a["monthly_fee"]) for a in addon_lines), Decimal(0))
    setup = (plan.setup_fee_per_station * stations).quantize(Q2)
    monthly = stations_monthly + addons_monthly
    return {
        "plan": plan.code, "currency": plan.currency, "stations": stations, "discount_percent": discount, "setup_fee": str(setup),
        "monthly_stations": str(stations_monthly), "addons": addon_lines, "monthly_total": str(monthly),
        "first_payment": str(setup + monthly * months), "months": months,
    }


# ---- payments --------------------------------------------------------------------------------------------------

def apply_payment(tenant, *, provider, event_id, amount, currency, months=1, today=None):
    """A successful payment: extend paid_through, mark active, and (if suspended) reactivate instantly."""
    today = today or timezone.localdate()
    with tenant_scope(tenant.id):
        sub = Subscription.objects.select_related("reseller").get(tenant_id=tenant.id)
        base = max(today, sub.paid_through) if sub.paid_through else today
        sub.paid_through = add_months(base, months)
        sub.status = Subscription.Status.ACTIVE
        sub.suspended_at = None
        sub.save(update_fields=["paid_through", "status", "suspended_at", "updated_at"])
        reactivated = False
        if tenant.status == Tenant.Status.SUSPENDED:
            tenant.status = Tenant.Status.ACTIVE
            tenant.save(update_fields=["status", "updated_at"])
            reactivated = True
        share = (amount * sub.reseller.revenue_share_pct / 100).quantize(Q2) if sub.reseller_id else Decimal(0)
        payment = Payment.objects.create(
            tenant=tenant, provider=provider, event_id=event_id, status="succeeded", amount=amount, currency=currency, months=months,
            paid_through_after=sub.paid_through, reseller=sub.reseller, reseller_share=share,
        )
        audit.log("payment_received", tenant_id=tenant.id, obj=payment, changes={"amount": str(amount), "currency": currency, "paid_through": sub.paid_through.isoformat(), "reactivated": reactivated}, via_platform=False)
    return payment, reactivated


def process_event(event, provider_name):
    """Handle one verified webhook event exactly once. Must run with the platform context on."""
    try:
        with transaction.atomic():
            record = PaymentEvent.objects.create(provider=provider_name, event_id=event.id, type=event.type)
    except IntegrityError:
        return {"status": "duplicate"}
    result = {"status": "ignored"}
    if event.type == "payment.succeeded":
        tenant = None
        if event.tenant_id:
            tenant = Tenant.objects.filter(pk=event.tenant_id).first()
            if tenant is None:
                record.result = "unknown_tenant"
                record.save(update_fields=["result", "updated_at"])
                return {"status": "unknown_tenant"}
        elif event.customer:
            try:
                tenant = _signup(event, provider_name)
                result = {"status": "tenant_created", "tenant_id": str(tenant.pk)}
            except (IntegrityError, KeyError, TypeError, ValueError) as exc:
                # Money arrived but the account could not be created (e.g. owner email already used, or a bad payload).
                # Nothing half-created is kept; the event is recorded so staff can follow up. See docs/runbook.md.
                log.error("billing signup failed for event %s: %s", event.id, exc)
                tenant, result = None, {"status": "signup_conflict"}
        if tenant is not None:
            payment, reactivated = apply_payment(tenant, provider=provider_name, event_id=event.id, amount=event.amount, currency=event.currency, months=event.months)
            result = {**result, "status": result["status"] if result["status"] == "tenant_created" else ("reactivated" if reactivated else "paid"), "paid_through": payment.paid_through_after.isoformat()}
            record.tenant = tenant
    elif event.type == "payment.failed" and event.tenant_id:
        tenant = Tenant.objects.filter(pk=event.tenant_id).first()
        if tenant:
            with tenant_scope(tenant.id):
                Payment.objects.create(tenant=tenant, provider=provider_name, event_id=event.id, status="failed", amount=event.amount, currency=event.currency, months=event.months)
                audit.log("payment_failed", tenant_id=tenant.id, changes={"amount": str(event.amount)}, via_platform=False)
            record.tenant = tenant
            result = {"status": "failed_recorded"}
    record.result = result["status"]
    record.save(update_fields=["tenant", "result", "updated_at"])
    return result


def _signup(event, provider_name):
    """A new customer paid: create their business, owner login and a welcome invite."""
    c = event.customer
    reseller = Reseller.objects.filter(pk=c["reseller"]).first() if c.get("reseller") else None
    tenant, owner, temp = create_tenant_with_owner(
        business_name=c["business_name"], owner_name=c["owner_name"], owner_email=(c.get("owner_email") or None), owner_phone=(c.get("owner_phone") or None),
        plan=c.get("plan", ""), stations=[{"name": n} for n in c.get("stations", [])], source="billing",
    )
    with tenant_scope(tenant.id):
        sub = Subscription.objects.get(tenant_id=tenant.id)
        sub.reseller, sub.addons = reseller, list(c.get("addons", []))
        sub.save(update_fields=["reseller", "addons", "updated_at"])
        who = owner.phone or owner.email
        send_message(
            tenant_id=tenant.id, channel="sms" if owner.phone else "none", to=owner.phone or "", kind="welcome_invite",
            body=f"Welcome to PumpWise, {owner.full_name.split()[0]}!\nSign in: {settings.FRONTEND_BASE_URL}\nLogin: {who}\nTemporary password: {temp}\nYou will choose your own password at first sign in.\nBuilt by Tendeka & Elichi",
        )
    return tenant


# ---- the daily cycle: reminders, then suspension -------------------------------------------------------------------

def days_past_due(sub, today):
    return (today - sub.paid_through).days if sub.paid_through and sub.paid_through < today else 0


def _notify(tenant, owner, text, kind):
    cfg = get_settings(tenant.id)
    phone = cfg.summary_phone or (owner.phone if owner else "")
    channel = cfg.summary_channel if cfg.summary_channel != "none" else "sms"
    send_message(tenant_id=tenant.id, channel=channel, to=phone, kind=kind, body=text)
    if owner and owner.email:
        try:
            send_mail("PumpWise: subscription", text, settings.DEFAULT_FROM_EMAIL, [owner.email], fail_silently=True)
        except Exception:  # noqa: BLE001
            log.warning("billing email failed for %s", tenant.pk)


def run_billing_cycle(today=None):
    """For every tenant with a due date: day 1/3/7 past due -> reminder; at SUSPEND_AFTER_DAYS -> suspend (lock owner/manager)."""
    from apps.accounts.models import Role, User
    from apps.core.db import db_context

    today = today or timezone.localdate()
    summary = {"reminders": 0, "suspended": 0}
    with db_context(platform_admin=True):
        subs = list(Subscription.objects.select_related("tenant").filter(paid_through__isnull=False, paid_through__lt=today).exclude(tenant__status="cancelled"))
    for sub in subs:
        tenant, late = sub.tenant, days_past_due(sub, today)
        try:
            with db_context(platform_admin=True), tenant_scope(tenant.id):
                owner = User.objects.filter(role=Role.OWNER).order_by("created_at").first()
                if sub.status == Subscription.Status.ACTIVE:
                    sub.status = Subscription.Status.PAST_DUE
                    sub.save(update_fields=["status", "updated_at"])
                if late >= settings.BILLING_SUSPEND_AFTER_DAYS and tenant.status == Tenant.Status.ACTIVE:
                    tenant.status = Tenant.Status.SUSPENDED
                    tenant.save(update_fields=["status", "updated_at"])
                    sub.status, sub.suspended_at = Subscription.Status.SUSPENDED, timezone.now()
                    sub.save(update_fields=["status", "suspended_at", "updated_at"])
                    BillingReminder.objects.get_or_create(tenant=tenant, due_date=sub.paid_through, stage=settings.BILLING_SUSPEND_AFTER_DAYS, defaults={"kind": "suspension"})
                    audit.log("tenant_auto_suspended", tenant_id=tenant.id, obj=tenant, changes={"days_past_due": late, "paid_through": sub.paid_through.isoformat()}, via_platform=False)
                    _notify(tenant, owner, "Your PumpWise subscription is overdue, so owner and manager access is paused. Attendants can keep recording sales. Pay to reactivate instantly. Your records are safe.", "billing_suspended")
                    summary["suspended"] += 1
                    continue
                stage = max((d for d in settings.BILLING_REMINDER_DAYS if late >= d), default=None)
                if stage and not BillingReminder.objects.filter(tenant=tenant, due_date=sub.paid_through, stage=stage).exists():
                    BillingReminder.objects.create(tenant=tenant, due_date=sub.paid_through, stage=stage)
                    left = settings.BILLING_SUSPEND_AFTER_DAYS - late
                    _notify(tenant, owner, f"Reminder: your PumpWise subscription was due on {sub.paid_through:%d %b}. Please pay within {left} days to avoid a pause in access.", "billing_reminder")
                    summary["reminders"] += 1
        except Exception:  # noqa: BLE001 - one tenant must never stop the cycle
            log.exception("billing cycle failed for tenant %s", tenant.pk)
    return summary


def banner_for(sub, tenant, today=None):
    """What the app shows owners/managers: nothing, a warning, a critical notice, or the suspended screen."""
    today = today or timezone.localdate()
    late = days_past_due(sub, today) if sub else 0
    if tenant.status == "suspended":
        return {"level": "suspended", "days_past_due": late, "message": "Access is paused. Pay to reactivate; your records are safe."}
    if late >= 1:
        left = max(settings.BILLING_SUSPEND_AFTER_DAYS - late, 0)
        return {"level": "critical" if late >= 7 else "warning", "days_past_due": late, "message": f"Subscription overdue by {late} day{'s' if late != 1 else ''}. Access pauses in {left} day{'s' if left != 1 else ''}."}
    if sub and sub.paid_through and (sub.paid_through - today) <= timedelta(days=3):
        return {"level": "info", "days_past_due": 0, "message": f"Subscription renews on {sub.paid_through:%d %b}."}
    return None
