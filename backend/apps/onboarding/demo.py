"""Demo and trial businesses (PLAN.md Phases 11 and 12).

The data comes from mock_data/seed_demo.json (made by mock_data/generate_mock_data.py) and is replayed through the
real services (shifts, readings, cash, dips, deliveries, prices) inside the tenant's database context, so Row-Level
Security applies and every screen, report, rollup and alert behaves exactly as it will for a real customer.

Safety rules:
* only businesses with is_demo = true can ever be wiped and reloaded (`wipe_demo_data` checks it in the database,
  and Row-Level Security limits the delete to that one tenant's rows even if a statement were wrong);
* the wipe keeps the business, its logins, settings, subscription, payments and audit log, and removes the
  operational demo data, so the same logins keep working after the nightly reload;
* demo businesses never send real SMS, WhatsApp or email and use the billing sandbox (see messaging and billing).
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.utils import generate_temporary_password
from apps.audit import services as audit
from apps.core.db import db_context, tenant_scope
from apps.insights.alerts import evaluate_tenant
from apps.operations import services as ops
from apps.operations.rollups import deferred
from apps.stations.models import CalibrationPoint, ExchangeRate, FuelType, Nozzle, Pump, Station, Tank
from apps.stations.services import set_price
from apps.tenants.features import set_station_policy, set_tenant_features
from apps.tenants.models import Subscription, Tenant
from apps.tenants.services import DEFAULT_FUEL_TYPES, create_tenant_with_owner, get_settings

log = logging.getLogger(__name__)
DEMO_FILE = Path(settings.BASE_DIR) / "mock_data" / "seed_demo.json"
TRIAL_DAYS, TRIAL_DELETE_AFTER_DAYS = 14, 30
D = Decimal

# Kept when a demo business is wiped and reloaded: who it is, who can log in, what it pays, and its history.
KEEP_ON_WIPE = {"accounts_user", "tenants_tenantsettings", "tenants_subscription", "audit_auditlog",
                "billing_payment", "billing_billingreminder", "billing_paymentevent", "exports_tenantexport"}


class NotDemoTenant(Exception):
    """Raised before anything is deleted when asked to wipe a business that is not a demo."""


# ---- the data ----------------------------------------------------------------------------------------------------

def generate(*, end=None, days=30, seed=7, name="Demo Fuel Services"):
    """Fresh mock data with --end = yesterday by default, as plain JSON values (exactly what the file holds)."""
    from mock_data.generate_mock_data import generate as make

    return json.loads(json.dumps(make(end=end or timezone.localdate() - timedelta(days=1), days=days, seed=seed, name=name)))


def load_file(path=None):
    return json.loads(Path(path or DEMO_FILE).read_text(encoding="utf-8"))


def write_file(data, path=None):
    Path(path or DEMO_FILE).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def _local(text):
    return timezone.make_aware(datetime.strptime(text, "%Y-%m-%dT%H:%M"), timezone.get_current_timezone())


# ---- creating, loading, wiping -------------------------------------------------------------------------------------

def create_demo_tenant(*, name, password=None, owner_email=None, owner_phone=None, trial_days=None, actor=None, request=None):
    """A new demo (or trial) business with the paid features on. Returns (tenant, owner, password).

    Call with the platform context on (platform endpoints are; `seed_demo_tenant` sets it)."""
    token = uuid.uuid4().hex[:6]
    today = timezone.localdate()
    tenant, owner, temporary = create_tenant_with_owner(
        business_name=name, owner_name="Demo Owner", owner_email=owner_email or (None if owner_phone else f"owner.{token}@demo.example"),
        owner_phone=owner_phone, stations=[], source="platform", actor=actor, request=request,
    )
    expires = today + timedelta(days=trial_days) if trial_days else None
    Tenant.objects.filter(pk=tenant.pk).update(is_demo=True, trial_expires_at=expires)
    tenant.refresh_from_db()
    Subscription.objects.filter(tenant_id=tenant.id).update(plan="trial" if trial_days else "demo", paid_through=expires or today + timedelta(days=365))
    set_tenant_features(tenant=tenant, multi_attendant=True, pump_assignment=True)
    if password:
        with tenant_scope(tenant.id):
            owner = User.objects.get(pk=owner.pk)
            owner.set_password(password)
            owner.must_change_password = False
            owner.save()
    return tenant, owner, password or temporary


def load_demo(tenant, data, *, password=None):
    """Replay the mock data into a demo business through the real services. Returns the staff logins."""
    if not tenant.is_demo:
        raise NotDemoTenant(str(tenant.pk))
    with db_context(tenant_id=tenant.id), deferred():
        owner = User.objects.filter(role=Role.OWNER).order_by("created_at").first()
        staff = {}
        for s in data["staff"]:
            user = User.objects.filter(role=s["role"], full_name=s["full_name"]).first()
            if user is None:
                user = User.objects.create_user(tenant_id=tenant.id, role=s["role"], full_name=s["full_name"], email=f"{s['key']}.{tenant.slug}@demo.example",
                                                password=password or generate_temporary_password())
            staff[s["key"]] = user
        cfg = get_settings(tenant.id)
        cfg.summary_phone, cfg.summary_channel, cfg.onboarding_flags = data["business"]["summary_phone"], "sms", {**cfg.onboarding_flags, "qr_printed": True}
        cfg.save()
        fuel = {f.code: f for f in FuelType.objects.all()}
        for fname, code in DEFAULT_FUEL_TYPES:
            if code not in fuel:
                fuel[code] = FuelType.objects.create(tenant_id=tenant.id, name=fname, code=code)
        for r in data["exchange_rates"]:
            ExchangeRate.objects.create(tenant_id=tenant.id, from_currency=r["from_currency"], to_currency=r["to_currency"], rate=D(r["rate"]),
                                        effective_from=_local(r["effective_from"]), created_by=owner)

        stations, tanks, pumps, nozzles = {}, {}, {}, {}
        for s in data["stations"]:
            st = Station.objects.create(tenant_id=tenant.id, name=s["name"], address=s["address"], pricing_currency=s["pricing_currency"])
            if (s["attendants_mode"], s["assignment_mode"]) != ("single", "shared"):  # the platform sets the paid policy at setup
                with db_context(platform_admin=True):
                    set_station_policy(station=st, attendants_mode=s["attendants_mode"], assignment_mode=s["assignment_mode"])
                st.refresh_from_db()
            stations[s["key"]] = st
            for t in s["tanks"]:
                tank = Tank.objects.create(tenant_id=tenant.id, station=st, name=t["name"], fuel_type=fuel[t["fuel"]], capacity_litres=D(t["capacity"]), reserve_litres=D(t["reserve"]))
                for dip_mm, litres in t["calibration"]:
                    CalibrationPoint.objects.create(tenant_id=tenant.id, tank=tank, dip_mm=dip_mm, litres=litres)
                tanks[t["key"]] = tank
            for p in s["pumps"]:
                pump = Pump.objects.create(tenant_id=tenant.id, station=st, tank=tanks[p["tank"]], name=p["name"], brand=p["brand"], model=p["model"], year=p["year"])
                pumps[p["key"]] = pump
                for n in p["nozzles"]:
                    nozzles[f"{p['key']}:{n['number']}"] = Nozzle.objects.create(tenant_id=tenant.id, pump=pump, number=n["number"])
            for pr in s["prices"]:
                set_price(tenant_id=tenant.id, station=st, fuel_type=fuel[pr["fuel"]], currency=pr["currency"], price=D(pr["price"]), effective_from=_local(pr["from"]), user=owner)

        manager = staff["manager"]

        def readings(values):
            return [{"nozzle": nozzles[k], "reading": D(v)} for k, v in values.items()]

        def run_shift(ev, start_at, end_at):
            lead = staff[ev["lead"]]
            assignments = {str(pumps[p].pk): staff[who].pk for p, who in ev.get("assignments", {}).items()}
            shift = ops.start_shift(user=lead, station=stations[ev["station"]], started_at=start_at, readings=readings(ev["start_readings"]),
                                    attendants=[staff[k] for k in ev.get("attendants", [])], assignments=assignments or None)
            if end_at is None:
                return
            for who, cash in ev["cash"].items():  # the others hand in first; the lead's 'end' then closes the shift
                if who != ev["lead"]:
                    ops.end_shift(user=staff[who], shift=shift, readings=[], cash=cash, ended_at=end_at)
            ops.end_shift(user=lead, shift=shift, readings=readings(ev["end_readings"]), cash=ev["cash"].get(ev["lead"], []), ended_at=end_at)

        def apply(ev, at=None, start_at=None, end_at=None):
            if ev["type"] == "dip":
                ops.record_tank_dip(user=manager, tank=tanks[ev["tank"]], litres=D(ev["litres"]), at=at)
            elif ev["type"] == "delivery":
                ops.record_delivery(user=manager, tank=tanks[ev["tank"]], litres=D(ev["litres"]), delivered_at=at, supplier=ev["supplier"], note_number=ev["note_number"])
            elif ev["type"] in ("shift", "open_shift"):
                run_shift(ev, start_at, end_at)

        for ev in data["events"]:
            apply(ev, at=_local(ev["at"]) if "at" in ev else None,
                  start_at=_local(ev["start"]) if ev.get("start") else None, end_at=_local(ev["end"]) if ev.get("end") else None)
        now = timezone.now()
        for ev in data["live"]:  # relative to now, so the demo always has a shift open and today's sales
            ago = {k: now - timedelta(minutes=ev[k]) for k in ("minutes_ago", "start_minutes_ago", "end_minutes_ago") if k in ev}
            apply(ev, at=ago.get("minutes_ago"), start_at=ago.get("start_minutes_ago"), end_at=ago.get("end_minutes_ago"))
        evaluate_tenant(tenant.id)
    return staff


def wipe_demo_data(tenant_id):
    """Delete a DEMO business's operational data so it can be reloaded. Refuses (before deleting anything) for any
    business that is not marked is_demo. Runs on the owner connection (the app role can never delete), inside that
    tenant's Row-Level Security context, in one transaction."""
    tid = str(tenant_id)
    with transaction.atomic(using="owner"), connections["owner"].cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, true), set_config('app.platform_admin', 'on', true)", [tid])
        cur.execute("SELECT is_demo FROM tenants_tenant WHERE id = %s", [tid])
        row = cur.fetchone()
        if not row or row[0] is not True:
            raise NotDemoTenant(tid)
        cur.execute("SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema = 'public' AND column_name = 'tenant_id' ORDER BY 1")
        tables = [t for (t,) in cur.fetchall() if t not in KEEP_ON_WIPE]
        removed = 0
        for t in tables:  # foreign keys are checked at commit, so the order does not matter
            cur.execute(f'DELETE FROM "{t}" WHERE tenant_id = %s', [tid])
            removed += cur.rowcount
    return removed


def reload_demo_tenant(tenant_id, data):
    """Wipe and reload one demo business. Logins, settings and subscription are kept."""
    with db_context(platform_admin=True):
        tenant = Tenant.objects.get(pk=tenant_id)
    if not tenant.is_demo:
        raise NotDemoTenant(str(tenant_id))
    wipe_demo_data(tenant.id)
    load_demo(tenant, data)
    return tenant


def seed_demo_tenant(*, name="Demo Fuel Services", days=30, seed=7, password, data=None):
    """Create a demo business and fill it (sales presentations, staging, tests). Deterministic for a given seed."""
    data = data or generate(days=days, seed=seed, name=name)
    with db_context(platform_admin=True):
        tenant, owner, _ = create_demo_tenant(name=name, password=password)
    staff = load_demo(tenant, data, password=password)
    from apps.billing.services import apply_payment

    with db_context(platform_admin=True):
        apply_payment(Tenant.objects.get(pk=tenant.id), provider="demo", event_id=f"demo-{tenant.id}", amount=D("86.00"), currency="USD", months=12)
    return {"tenant_id": str(tenant.id), "slug": tenant.slug, "password": password,
            "logins": {"owner": owner.email, **{k: u.email for k, u in staff.items()}, "attendant": staff["att1"].email}}


# ---- trials -------------------------------------------------------------------------------------------------------

def create_trial_tenant(*, prospect_name, owner_email=None, owner_phone=None, actor=None, request=None, days=14):
    """Platform action: a demo business for a prospect, with an owner login (random password, shown once) that
    expires after TRIAL_DAYS. Must run with the platform context on."""
    tenant, owner, password = create_demo_tenant(name=prospect_name, owner_email=owner_email, owner_phone=owner_phone,
                                                 trial_days=TRIAL_DAYS, actor=actor, request=request)
    load_demo(tenant, generate(days=days, name=prospect_name))
    return tenant, owner, password


def _suspend(tenant, reason):
    with db_context(platform_admin=True), tenant_scope(tenant.id):
        Tenant.objects.filter(pk=tenant.pk).update(status=Tenant.Status.SUSPENDED)
        Subscription.objects.filter(tenant_id=tenant.id).update(status=Subscription.Status.SUSPENDED, suspended_at=timezone.now())
        audit.log("trial_expired", tenant_id=tenant.id, obj=tenant, via_platform=True, changes={"reason": reason})


def remove_trial(tenant):
    """An expired trial that was not converted: its demo data is wiped, logins switched off, the business soft-deleted."""
    wipe_demo_data(tenant.id)
    with db_context(platform_admin=True), tenant_scope(tenant.id):
        for user in User.objects.all():
            user.deactivate()
        Tenant.objects.filter(pk=tenant.pk).update(status=Tenant.Status.CANCELLED, deleted_at=timezone.now())
        audit.log("trial_removed", tenant_id=tenant.id, obj=tenant, via_platform=True, changes={"expired": str(tenant.trial_expires_at)})


def run_trial_lifecycle(today=None):
    """Daily: expired trials are suspended; TRIAL_DELETE_AFTER_DAYS later, if not converted, they are removed."""
    today = today or timezone.localdate()
    with db_context(platform_admin=True):
        trials = list(Tenant.objects.filter(is_demo=True, trial_expires_at__isnull=False))
    out = {"suspended": 0, "removed": 0}
    for t in trials:
        try:
            if t.trial_expires_at + timedelta(days=TRIAL_DELETE_AFTER_DAYS) < today:
                remove_trial(t)
                out["removed"] += 1
            elif t.trial_expires_at < today and t.status == Tenant.Status.ACTIVE:
                _suspend(t, "trial ended")
                out["suspended"] += 1
        except Exception:  # noqa: BLE001 - one bad trial never stops the others
            log.exception("trial lifecycle failed for %s", t.pk)
    return out


def convert_trial(tenant, *, actor=None, request=None):
    """The prospect becomes a customer: the demo data goes, the business stays (logins, settings) and is no longer a demo."""
    if not tenant.is_demo:
        raise NotDemoTenant(str(tenant.pk))
    wipe_demo_data(tenant.id)
    with tenant_scope(tenant.id):
        Tenant.objects.filter(pk=tenant.pk).update(is_demo=False, trial_expires_at=None, status=Tenant.Status.ACTIVE)
        Subscription.objects.filter(tenant_id=tenant.id).update(status=Subscription.Status.ACTIVE, suspended_at=None)
        audit.log("trial_converted", actor=actor, tenant_id=tenant.id, obj=tenant, request=request)
    tenant.refresh_from_db()
    return tenant
