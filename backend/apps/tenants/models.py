from decimal import Decimal

from django.db import models

from apps.core.models import SoftDeleteModel, UUIDModel


class Tenant(UUIDModel, SoftDeleteModel):
    """A fuel-station business (customer). Root of the tenant hierarchy.

    This table is the one place where the tenant's own id (not a tenant_id column) is the
    isolation key. Subscription state arrives in Phase 8; `status` is the lock-out switch.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        CANCELLED = "cancelled", "Cancelled"

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=80, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Subscription(UUIDModel):
    """One per tenant. Platform staff set it by hand until Phase 8 automates it from payments.

    A tenant may READ its own row but never write it (see the RLS policies in the migration).
    Subscription status and Tenant.status are separate on purpose: status here says what the
    tenant has paid for; Tenant.status is the lock-out switch that actually blocks access.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        SUSPENDED = "suspended", "Suspended"
        CANCELLED = "cancelled", "Cancelled"

    tenant = models.OneToOneField(Tenant, on_delete=models.PROTECT, related_name="subscription")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    plan = models.CharField(max_length=60, blank=True, default="")
    paid_through = models.DateField(null=True, blank=True)
    reseller = models.ForeignKey("billing.Reseller", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    addons = models.JSONField(default=list, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)  # data is retained at least 90 days after this (docs/retention.md)

    def __str__(self):
        return f"{self.tenant_id}: {self.status}"


def logo_upload_to(instance, filename):
    return f"tenant/{instance.tenant_id}/branding/{filename}"


class TenantSettings(UUIDModel):
    """Per-tenant configuration: thresholds, branding, daily-summary preferences. One row per tenant."""

    tenant = models.OneToOneField(Tenant, on_delete=models.PROTECT, related_name="settings")
    display_name = models.CharField(max_length=200, blank=True, default="")
    logo = models.FileField(upload_to=logo_upload_to, null=True, blank=True)
    # Unexplained fuel loss is flagged above max(litres, percent of litres dispensed).
    loss_threshold_litres = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("20"))
    loss_threshold_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.5"))
    # Cash is flagged when expected and handed-in differ by more than max(amount, percent of expected).
    cash_variance_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("5"))
    cash_variance_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("1"))
    low_stock_days = models.PositiveSmallIntegerField(default=3)
    refill_lead_days = models.PositiveSmallIntegerField(default=2)
    summary_channel = models.CharField(max_length=10, default="sms", choices=[("sms", "SMS"), ("whatsapp", "WhatsApp"), ("none", "Off")])
    summary_phone = models.CharField(max_length=20, blank=True, default="")
    # Manual onboarding steps the technician ticks off (e.g. {"qr_printed": true})
    onboarding_flags = models.JSONField(default=dict, blank=True)
