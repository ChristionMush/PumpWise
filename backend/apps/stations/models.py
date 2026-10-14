import re
import secrets
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower

from apps.core.models import TenantOwnedModel

CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def validate_currency(value):
    if not CURRENCY_RE.match(value or ""):
        raise ValidationError("Use a 3-letter currency code such as USD or ZWG.")


def new_qr_token():
    return secrets.token_urlsafe(9)


def upload_to(instance, filename):
    return f"tenant/{instance.tenant_id}/{instance.__class__.__name__.lower()}/{instance.pk}/{filename}"


class Station(TenantOwnedModel):
    """A forecourt."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    class AttendantsMode(models.TextChoices):
        SINGLE = "single", "One attendant per shift"
        MULTIPLE = "multiple", "Several attendants per shift"

    class AssignmentMode(models.TextChoices):
        SHARED = "shared", "Everyone shares the pumps"
        ASSIGNED = "assigned", "Each pump is assigned to one attendant"

    name = models.CharField(max_length=120)
    address = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    # Sales are priced and totalled in this currency; other currencies are converted with the owner's exchange rate.
    pricing_currency = models.CharField(max_length=3, default="USD", validators=[validate_currency])
    # Set by the platform owner at setup, within what the tenant has been granted. A database trigger refuses any
    # other change (see migration 0005), so the tenant cannot switch itself to a paid mode.
    attendants_mode = models.CharField(max_length=10, choices=AttendantsMode.choices, default=AttendantsMode.SINGLE)
    assignment_mode = models.CharField(max_length=10, choices=AssignmentMode.choices, default=AssignmentMode.SHARED)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(F("tenant"), Lower("name"), condition=Q(deleted_at__isnull=True), name="station_name_unique_per_tenant"),
            models.CheckConstraint(condition=Q(assignment_mode="shared") | Q(attendants_mode="multiple"), name="station_assigned_needs_multiple"),
        ]

    def __str__(self):
        return self.name


class StationAssignment(TenantOwnedModel):
    """Limits a manager/attendant to some stations. A user with NO assignments sees all stations of the tenant."""

    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    station = models.ForeignKey(Station, on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(F("user"), F("station"), condition=Q(deleted_at__isnull=True), name="station_assignment_unique")]


class FuelType(TenantOwnedModel):
    name = models.CharField(max_length=60)
    code = models.CharField(max_length=20)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(F("tenant"), Lower("code"), condition=Q(deleted_at__isnull=True), name="fueltype_code_unique_per_tenant")]

    def __str__(self):
        return self.name


class Tank(TenantOwnedModel):
    class DataSource(models.TextChoices):
        MANUAL = "manual", "Manual dip"
        ATG = "atg", "Automatic tank gauge"

    station = models.ForeignKey(Station, on_delete=models.PROTECT, related_name="tanks")
    name = models.CharField(max_length=80)
    fuel_type = models.ForeignKey(FuelType, on_delete=models.PROTECT, related_name="+")
    capacity_litres = models.DecimalField(max_digits=12, decimal_places=3, validators=[MinValueValidator(1)])
    reserve_litres = models.DecimalField(max_digits=12, decimal_places=3, default=0, validators=[MinValueValidator(0)])
    data_source = models.CharField(max_length=10, choices=DataSource.choices, default=DataSource.MANUAL)
    qr_token = models.CharField(max_length=24, default=new_qr_token, editable=False)

    class Meta:
        ordering = ["station__name", "name"]
        constraints = [
            models.UniqueConstraint(F("station"), Lower("name"), condition=Q(deleted_at__isnull=True), name="tank_name_unique_per_station"),
            models.UniqueConstraint(F("tenant"), "qr_token", name="tank_qr_unique_per_tenant"),
            models.CheckConstraint(condition=Q(reserve_litres__lte=F("capacity_litres")), name="tank_reserve_within_capacity"),
        ]

    def __str__(self):
        return self.name


class CalibrationPoint(TenantOwnedModel):
    """One row of a tank's calibration chart: dip height (mm) -> litres."""

    tank = models.ForeignKey(Tank, on_delete=models.PROTECT, related_name="calibration")
    dip_mm = models.DecimalField(max_digits=8, decimal_places=1)
    litres = models.DecimalField(max_digits=12, decimal_places=3)

    class Meta:
        ordering = ["dip_mm"]
        constraints = [models.UniqueConstraint(F("tank"), "dip_mm", condition=Q(deleted_at__isnull=True), name="calibration_unique_dip")]


class Pump(TenantOwnedModel):
    class DataSource(models.TextChoices):
        MANUAL = "manual", "Manual"
        PULSE_READER = "pulse_reader", "Pulse reader"
        CONTROLLER = "controller", "Forecourt controller"

    station = models.ForeignKey(Station, on_delete=models.PROTECT, related_name="pumps")
    tank = models.ForeignKey(Tank, on_delete=models.PROTECT, related_name="pumps")
    name = models.CharField(max_length=80)
    # Hardware details, optional at first. Used later to research which pumps can be automated.
    brand = models.CharField(max_length=80, blank=True, default="")
    model = models.CharField(max_length=80, blank=True, default="")
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    nameplate_photo = models.FileField(upload_to=upload_to, null=True, blank=True)
    display_photo = models.FileField(upload_to=upload_to, null=True, blank=True)
    data_source = models.CharField(max_length=20, choices=DataSource.choices, default=DataSource.MANUAL)
    qr_token = models.CharField(max_length=24, default=new_qr_token, editable=False)

    class Meta:
        ordering = ["station__name", "name"]
        constraints = [
            models.UniqueConstraint(F("station"), Lower("name"), condition=Q(deleted_at__isnull=True), name="pump_name_unique_per_station"),
            models.UniqueConstraint(F("tenant"), "qr_token", name="pump_qr_unique_per_tenant"),
        ]

    def __str__(self):
        return self.name


class Nozzle(TenantOwnedModel):
    pump = models.ForeignKey(Pump, on_delete=models.PROTECT, related_name="nozzles")
    number = models.PositiveSmallIntegerField()
    label = models.CharField(max_length=40, blank=True, default="")
    # Meter rolls over to zero after this value.
    meter_max = models.DecimalField(max_digits=14, decimal_places=2, default=99999999.99)

    class Meta:
        ordering = ["pump__name", "number"]
        constraints = [models.UniqueConstraint(F("pump"), "number", condition=Q(deleted_at__isnull=True), name="nozzle_number_unique_per_pump")]

    def __str__(self):
        return self.label or f"Nozzle {self.number}"


class FuelPrice(TenantOwnedModel):
    """Price history per station, fuel type and currency. Never edited in place: a new price closes the previous one."""

    station = models.ForeignKey(Station, on_delete=models.PROTECT, related_name="+")
    fuel_type = models.ForeignKey(FuelType, on_delete=models.PROTECT, related_name="+")
    currency = models.CharField(max_length=3, validators=[validate_currency])
    price_per_litre = models.DecimalField(max_digits=12, decimal_places=4)
    effective_from = models.DateTimeField()
    effective_to = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-effective_from"]
        constraints = [
            models.CheckConstraint(condition=Q(price_per_litre__gt=0), name="fuelprice_positive"),
            models.CheckConstraint(condition=Q(effective_to__isnull=True) | Q(effective_to__gt=F("effective_from")), name="fuelprice_valid_period"),
            models.UniqueConstraint(F("station"), F("fuel_type"), "currency", "effective_from", name="fuelprice_unique_start"),
        ]
        indexes = [models.Index(fields=["station", "fuel_type", "currency", "effective_from"])]


class ExchangeRate(TenantOwnedModel):
    """Owner-set rate: 1 unit of from_currency = rate units of to_currency, from effective_from."""

    from_currency = models.CharField(max_length=3, validators=[validate_currency])
    to_currency = models.CharField(max_length=3, validators=[validate_currency])
    rate = models.DecimalField(max_digits=18, decimal_places=6, validators=[MinValueValidator(Decimal("0.000001"))])
    effective_from = models.DateTimeField()
    created_by = models.ForeignKey("accounts.User", null=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-effective_from"]
        constraints = [models.UniqueConstraint(F("tenant"), "from_currency", "to_currency", "effective_from", name="exchangerate_unique_start")]
