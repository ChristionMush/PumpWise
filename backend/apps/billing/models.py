from django.db import models
from django.db.models import F

from apps.core.models import UUIDModel


class PricingPlan(UUIDModel):
    """Platform-wide price list. Tenants may read it; only platform staff change it."""

    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=80)
    currency = models.CharField(max_length=3, default="USD")
    setup_fee_per_station = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # one-time
    monthly_fee_per_station = models.DecimalField(max_digits=10, decimal_places=2)
    # [{"min_stations": 3, "percent": 10}, ...]: the highest matching tier applies to the monthly station fees
    multi_station_discounts = models.JSONField(default=list, blank=True)
    # {"whatsapp_summary": {"name": "...", "monthly_fee": "5.00"}}
    addons = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)


class Reseller(UUIDModel):
    """A partner who earns a share of what their referred tenants pay. Platform-internal."""

    name = models.CharField(max_length=120)
    contact = models.CharField(max_length=200, blank=True, default="")
    revenue_share_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # configurable per reseller
    active = models.BooleanField(default=True)


class PaymentEvent(UUIDModel):
    """Every webhook event ever processed. The unique key is what makes webhooks idempotent."""

    provider = models.CharField(max_length=30)
    event_id = models.CharField(max_length=120)
    type = models.CharField(max_length=40)
    tenant = models.ForeignKey("tenants.Tenant", null=True, on_delete=models.PROTECT, related_name="+")
    result = models.CharField(max_length=60, blank=True, default="")

    class Meta:
        constraints = [models.UniqueConstraint(F("provider"), "event_id", name="paymentevent_unique")]


class Payment(UUIDModel):
    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=30)
    event_id = models.CharField(max_length=120)
    status = models.CharField(max_length=12)  # succeeded | failed
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    months = models.PositiveSmallIntegerField(default=1)
    paid_through_after = models.DateField(null=True, blank=True)
    reseller = models.ForeignKey(Reseller, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reseller_share = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(F("provider"), "event_id", name="payment_unique_event")]


class BillingReminder(UUIDModel):
    """Which reminder (day 1, 3, 7 past due, or the suspension notice) was sent for which due date."""

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.PROTECT, related_name="+")
    due_date = models.DateField()
    stage = models.PositiveSmallIntegerField()  # days past due the stage stands for
    kind = models.CharField(max_length=12, default="reminder")  # reminder | suspension

    class Meta:
        constraints = [models.UniqueConstraint(F("tenant"), "due_date", "stage", name="reminder_once")]
