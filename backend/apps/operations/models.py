import uuid

from django.db import models
from django.db.models import F, Q

from apps.core.models import TenantOwnedModel
from apps.stations.models import upload_to, validate_currency


class Shift(TenantOwnedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        REVIEWED = "reviewed", "Reviewed"

    # The id may be generated on the phone (offline), so later offline entries can refer to it.
    station = models.ForeignKey("stations.Station", on_delete=models.PROTECT, related_name="+")
    attendant = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    flags = models.JSONField(default=list, blank=True)  # things a manager should look at
    notes = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["station", "-started_at"])]

    def add_flag(self, flag):
        if flag not in self.flags:
            self.flags = [*self.flags, flag]


class PumpMeterReading(TenantOwnedModel):
    class Kind(models.TextChoices):
        START = "start", "Shift start"
        CHECKPOINT = "checkpoint", "Mid-shift (price change)"
        END = "end", "Shift end"

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="readings")
    nozzle = models.ForeignKey("stations.Nozzle", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    reading = models.DecimalField(max_digits=14, decimal_places=2)
    reading_at = models.DateTimeField()
    source = models.CharField(max_length=20, default="manual")
    photo = models.FileField(upload_to=upload_to, null=True, blank=True)
    recorded_by = models.ForeignKey("accounts.User", null=True, on_delete=models.PROTECT, related_name="+")
    corrected = models.BooleanField(default=False)
    flags = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["reading_at"]
        constraints = [
            models.UniqueConstraint(
                F("shift"), F("nozzle"), "kind", condition=Q(deleted_at__isnull=True) & ~Q(kind="checkpoint"), name="reading_unique_start_end"
            ),
            models.CheckConstraint(condition=Q(reading__gte=0), name="reading_not_negative"),
        ]
        indexes = [models.Index(fields=["nozzle", "kind", "-reading_at"])]


class TankReading(TenantOwnedModel):
    """A dip. Recorded only by a manager or owner (rule 11); `client_id` is made on the phone so a retry never duplicates it."""

    client_id = models.UUIDField(null=True, blank=True)
    tank = models.ForeignKey("stations.Tank", on_delete=models.PROTECT, related_name="+")
    shift = models.ForeignKey(Shift, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    dip_mm = models.DecimalField(max_digits=8, decimal_places=1, null=True, blank=True)
    litres = models.DecimalField(max_digits=12, decimal_places=3)
    reading_at = models.DateTimeField()
    source = models.CharField(max_length=20, default="manual")
    photo = models.FileField(upload_to=upload_to, null=True, blank=True)
    recorded_by = models.ForeignKey("accounts.User", null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-reading_at"]
        indexes = [models.Index(fields=["tank", "-reading_at"])]
        constraints = [models.UniqueConstraint(F("tenant"), "client_id", condition=Q(client_id__isnull=False), name="tankreading_client_id_unique")]


class Delivery(TenantOwnedModel):
    """Fuel received, recorded by the manager or owner who received it, never by an attendant (rule 11)."""

    client_id = models.UUIDField(null=True, blank=True)
    tank = models.ForeignKey("stations.Tank", on_delete=models.PROTECT, related_name="+")
    delivered_at = models.DateTimeField()
    litres_received = models.DecimalField(max_digits=12, decimal_places=3)
    supplier = models.CharField(max_length=120, blank=True, default="")
    note_number = models.CharField(max_length=60, blank=True, default="")
    photo = models.FileField(upload_to=upload_to, null=True, blank=True)  # optional photo of the delivery note
    recorded_by = models.ForeignKey("accounts.User", null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-delivered_at"]
        indexes = [models.Index(fields=["tank", "-delivered_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(litres_received__gt=0), name="delivery_positive"),
            models.UniqueConstraint(F("tenant"), "client_id", condition=Q(client_id__isnull=False), name="delivery_client_id_unique"),
        ]


class CashSubmission(TenantOwnedModel):
    class PaymentType(models.TextChoices):
        CASH = "cash", "Cash"
        MOBILE_MONEY = "mobile_money", "Mobile money (e.g. EcoCash)"
        CARD = "card", "Card / swipe"
        CREDIT = "credit", "Credit / coupon"

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="cash")
    payment_type = models.CharField(max_length=15, choices=PaymentType.choices)
    currency = models.CharField(max_length=3, validators=[validate_currency])
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(F("shift"), "payment_type", "currency", condition=Q(deleted_at__isnull=True), name="cash_unique_per_shift"),
            models.CheckConstraint(condition=Q(amount__gte=0), name="cash_not_negative"),
        ]


class Transaction(TenantOwnedModel):
    """Litres sold through one nozzle between two meter readings, priced at the price in force when the period began."""

    station = models.ForeignKey("stations.Station", on_delete=models.PROTECT, related_name="+")
    shift = models.ForeignKey(Shift, null=True, on_delete=models.PROTECT, related_name="transactions")
    nozzle = models.ForeignKey("stations.Nozzle", on_delete=models.PROTECT, related_name="+")
    fuel_type = models.ForeignKey("stations.FuelType", on_delete=models.PROTECT, related_name="+")
    litres = models.DecimalField(max_digits=12, decimal_places=3)
    price_per_litre = models.DecimalField(max_digits=12, decimal_places=4)
    currency = models.CharField(max_length=3, validators=[validate_currency])
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    started_at = models.DateTimeField()
    occurred_at = models.DateTimeField()
    source = models.CharField(max_length=20, default="manual")
    start_reading = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    end_reading = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["station", "-occurred_at"]), models.Index(fields=["nozzle", "-occurred_at"])]
        constraints = [
            models.UniqueConstraint(F("shift"), F("nozzle"), "occurred_at", condition=Q(deleted_at__isnull=True), name="transaction_unique_period"),
            models.CheckConstraint(condition=Q(litres__gte=0), name="transaction_litres_not_negative"),
        ]


class SyncOperation(TenantOwnedModel):
    """Idempotency log for offline sync: the same op_id is never applied twice."""

    op_id = models.UUIDField(default=uuid.uuid4)
    kind = models.CharField(max_length=30)
    status = models.CharField(max_length=10)  # applied | rejected
    result = models.JSONField(default=dict)
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(F("tenant"), "op_id", name="syncop_unique_per_tenant")]
