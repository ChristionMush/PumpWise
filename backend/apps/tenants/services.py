from .models import TenantSettings


def get_settings(tenant_id):
    """The tenant's settings row, created with defaults the first time it is needed (call inside the tenant's context)."""
    found = TenantSettings.objects.filter(tenant_id=tenant_id).first()
    if found:
        return found
    created = TenantSettings.objects.create(tenant_id=tenant_id)
    created.refresh_from_db()  # so numeric fields are Decimals, exactly as when read later
    return created


DEFAULT_FUEL_TYPES = [("Petrol (Unleaded)", "PETROL"), ("Diesel", "DIESEL")]


def unique_slug(name):
    from django.utils.text import slugify

    from .models import Tenant

    base = slugify(name)[:60] or "tenant"
    slug, n = base, 1
    while Tenant.all_objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


def create_tenant_with_owner(*, business_name, owner_name, owner_email=None, owner_phone=None, plan="", paid_through=None, stations=(), actor=None, request=None, source="platform",
                             pump_system="shared"):
    """Create a business: tenant + subscription + owner login + stations + defaults, atomically.

    Must run inside a transaction with the platform context on (platform endpoints and the billing webhook do).
    Returns (tenant, owner, temporary_password). The temporary password is returned, never stored or logged in clear.
    """
    from django.db import transaction

    from apps.accounts.models import Role, User
    from apps.accounts.utils import generate_temporary_password, normalize_email, normalize_phone
    from apps.audit import services as audit
    from apps.core.db import tenant_scope
    from apps.stations.models import FuelType, Station

    from .models import Subscription, Tenant, TenantSettings

    # Every caller (platform screen, billing webhook) gets the same cleaning, so login always finds the owner.
    owner_email, owner_phone = normalize_email(owner_email), normalize_phone(owner_phone)
    if not owner_email and not owner_phone:
        raise ValueError("The owner needs an email or a valid phone number.")
    temporary_password = generate_temporary_password()
    with transaction.atomic():
        tenant = Tenant.objects.create(name=business_name, slug=unique_slug(business_name))
        Subscription.objects.create(tenant=tenant, plan=plan, paid_through=paid_through)
        with tenant_scope(tenant.id):
            owner = User.objects.create_user(
                tenant_id=tenant.id, role=Role.OWNER, full_name=owner_name, email=owner_email, phone=owner_phone,
                password=temporary_password, must_change_password=True,
            )
            for st in stations:
                Station.objects.create(tenant_id=tenant.id, name=st["name"], address=st.get("address", ""))
            for name, code in DEFAULT_FUEL_TYPES:
                FuelType.objects.create(tenant_id=tenant.id, name=name, code=code)
            TenantSettings.objects.create(tenant_id=tenant.id, summary_phone=owner_phone or "")
            audit.log(
                "tenant_created", actor=actor, tenant_id=tenant.id, obj=tenant, request=request, via_platform=source == "platform",
                changes={"name": tenant.name, "owner": owner.full_name, "stations": [s["name"] for s in stations], "source": source, "pump_system": pump_system},
            )
        if pump_system != "shared":
            from .features import set_pump_system

            set_pump_system(tenant=tenant, system=pump_system, actor=actor, request=request)
    return tenant, owner, temporary_password
