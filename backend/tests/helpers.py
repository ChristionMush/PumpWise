import uuid

from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.accounts.tokens import issue_tokens
from apps.core.db import db_context
from apps.tenants.models import Tenant

PASSWORD = "Sup3r-secret-pw"


def make_tenant(name="Acme Fuel"):
    from apps.tenants.models import Subscription

    with db_context(platform_admin=True):
        tenant = Tenant.objects.create(name=name, slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}")
        Subscription.objects.create(tenant=tenant)
        return tenant


def make_user(tenant, role, *, email=None, phone=None, full_name=None, password=PASSWORD):
    full_name = full_name or f"{role} {uuid.uuid4().hex[:4]}"
    if email is None and phone is None:
        email = f"{uuid.uuid4().hex[:10]}@example.com"
    ctx = {"tenant_id": tenant.id} if tenant else {"platform_admin": True}
    with db_context(**ctx):
        return User.objects.create_user(
            tenant_id=tenant.id if tenant else None, role=role, full_name=full_name, email=email, phone=phone, password=password
        )


def client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user)['access']}")
    return client


def make_platform_admin(**kw):
    return make_user(None, Role.PLATFORM_ADMIN, **kw)


def two_tenants():
    """Tenant A and B, each with an owner, manager and attendant."""
    out = {}
    for key in ("a", "b"):
        t = make_tenant(f"Tenant {key.upper()}")
        out[key] = {
            "tenant": t,
            Role.OWNER: make_user(t, Role.OWNER, full_name=f"Owner {key}"),
            Role.MANAGER: make_user(t, Role.MANAGER, full_name=f"Manager {key}"),
            Role.ATTENDANT: make_user(t, Role.ATTENDANT, full_name=f"Attendant {key}"),
        }
    return out["a"], out["b"]


def make_station(tenant, name="Main Forecourt", address=""):
    from apps.stations.models import Station

    with db_context(tenant_id=tenant.id):
        return Station.objects.create(tenant_id=tenant.id, name=name, address=address)


def platform_client():
    return client_for(make_platform_admin())


NEW_TENANT = {
    "business_name": "Msasa Fuel Services",
    "owner_name": "Rudo Chikwanha",
    "owner_phone": "0772123456",
    "plan": "standard",
    "stations": [{"name": "Msasa Road", "address": "Msasa, Harare"}, {"name": "Borrowdale"}],
}


def create_tenant_via_api(client=None, **overrides):
    client = client or platform_client()
    res = client.post("/api/platform/tenants/", {**NEW_TENANT, **overrides}, format="json")
    return client, res


# ---- forecourt factory ------------------------------------------------------------------------------------------
from datetime import UTC, datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from types import SimpleNamespace  # noqa: E402

UTC = UTC


def at(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def make_forecourt(tenant, *, price="1.50", currency="USD", price_from=None, nozzles=2, name="Main Station", code="PETROL"):
    from django.utils import timezone

    from apps.stations.models import CalibrationPoint, FuelType, Nozzle, Pump, Station, Tank
    from apps.stations.services import set_price

    with db_context(tenant_id=tenant.id):
        station = Station.objects.create(tenant_id=tenant.id, name=name, pricing_currency=currency)
        fuel = FuelType.objects.create(tenant_id=tenant.id, name=code.title(), code=code)
        tank = Tank.objects.create(tenant_id=tenant.id, station=station, name="Tank 1", fuel_type=fuel, capacity_litres=10000, reserve_litres=500)
        for dip, litres in ((0, 0), (500, 5000), (1000, 10000)):
            CalibrationPoint.objects.create(tenant_id=tenant.id, tank=tank, dip_mm=dip, litres=litres)
        pump = Pump.objects.create(tenant_id=tenant.id, station=station, name="Pump 1")
        # Every nozzle on the one tank keeps the tank arithmetic simple; see second_tank() for a second fuel.
        nz = [Nozzle.objects.create(tenant_id=tenant.id, pump=pump, tank=tank, number=i + 1) for i in range(nozzles)]
        if price:
            set_price(tenant_id=tenant.id, station=station, fuel_type=fuel, currency=currency, price=Decimal(price), effective_from=price_from or timezone.now() - timedelta(days=60))
    return SimpleNamespace(station=station, fuel=fuel, tank=tank, pump=pump, nozzles=nz)


def run_shift(tenant, attendant, fc, start, end, *, started_at, ended_at, cash=(), checkpoint=None, shift_id=None, approve=True):
    """Run a complete shift through the real services. start/end are per-nozzle reading lists. With approve (the default)
    a manager's approval of the end follows, as it would before the attendant's next shift."""
    from apps.operations import services

    with db_context(tenant_id=tenant.id):
        shift = services.start_shift(
            user=attendant, station=fc.station, started_at=started_at, shift_id=shift_id,
            readings=[{"nozzle": n, "reading": Decimal(str(r))} for n, r in zip(fc.nozzles, start, strict=False)],
        )
        if checkpoint:
            for n, r in zip(fc.nozzles, checkpoint[1], strict=False):
                services.add_checkpoint(user=attendant, shift=shift, nozzle=n, reading=Decimal(str(r)), at=checkpoint[0])
        services.end_shift(
            user=attendant, shift=shift, ended_at=ended_at,
            readings=[{"nozzle": n, "reading": Decimal(str(r))} for n, r in zip(fc.nozzles, end, strict=False)],
            cash=[{"payment_type": t, "currency": c, "amount": Decimal(str(a))} for t, c, a in cash],
        )
        if approve:
            approve_ends(attendant)
        return shift


def approve_ends(*people):
    """A manager approves these attendants' waiting ends of shift (call inside the tenant's db_context)."""
    from django.utils import timezone

    from apps.operations.models import ShiftAttendant

    ShiftAttendant.objects.filter(user__in=people, approval__in=["pending", "sent_back"]).update(approval="approved", approval_at=timezone.now())


def dip(tenant, user, fc, litres, when):
    from apps.operations import services

    with db_context(tenant_id=tenant.id):
        return services.record_tank_dip(user=user, tank=fc.tank, litres=Decimal(str(litres)), at=when)


def second_tank(fc, *, code="DIESEL", name="Tank 2"):
    """Another tank (and fuel) at a make_forecourt station, so a pump can sell two fuels."""
    from apps.stations.models import FuelType, Tank

    tenant_id = fc.station.tenant_id
    with db_context(tenant_id=tenant_id):
        fuel = FuelType.objects.filter(code=code).first() or FuelType.objects.create(tenant_id=tenant_id, name=code.title(), code=code)
        return Tank.objects.create(tenant_id=tenant_id, station=fc.station, name=name, fuel_type=fuel, capacity_litres=10000, reserve_litres=500)
