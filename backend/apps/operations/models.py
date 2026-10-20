import uuid

from django.db import models
from django.db.models import F, Q

from apps.core.models import TenantOwnedModel
from apps.stations.models import upload_to, validate_currency


class Shift(TenantOwnedModel):
    """One shift at one station. The people on it are ShiftAttendant rows: never a single attendant on the shift."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        REVIEWED = "reviewed", "Reviewed"

    # The id may be generated on the phone (offline), so later offline entries can refer to it.
    station = models.ForeignKey("stations.Station", on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    flags = models.JSONField(default=list, blank=True)  # things a manager should look at
    notes = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # The lead (or a manager) asked to close while someone still had to hand in: it closes by itself when they do.
    close_requested_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["station", "-started_at"])]

    def add_flag(self, flag):
        if flag not in self.flags:
            self.flags = [*self.flags, flag]

    def remove_flags(self, prefix):
        self.flags = [f for f in self.flags if not f.startswith(prefix)]


class ShiftAttendant(TenantOwnedModel):
    """A person's part of a shift. Joining and leaving mid-shift are joined_at / left_at; nothing is ever deleted.

    Usually one row per person per shift. On assigned pumps someone who ended their part (and was approved) can start
    again on the same open shift, on the pumps they now have: that is a second row, a second part, with its own pumps,
    cash and approval. At most one part per person is open (not left) at a time.

    An attendant's end of shift waits for a manager (approval): until it is approved they cannot start or join another
    shift. A manager may send it back to fix; changing it otherwise needs the owner's permission (UndoRequest)."""

    class Approval(models.TextChoices):
        NONE = "", "Not ended"
        PENDING = "pending", "Waiting for the manager"
        APPROVED = "approved", "Approved"
        SENT_BACK = "sent_back", "Sent back to fix"

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="attendants")
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    is_lead = models.BooleanField(default=False)
    joined_at = models.DateTimeField()
    left_at = models.DateTimeField(null=True, blank=True)
    cash_submitted_at = models.DateTimeField(null=True, blank=True)
    approval = models.CharField(max_length=10, choices=Approval.choices, default="", blank=True)
    approval_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    approval_at = models.DateTimeField(null=True, blank=True)
    approval_note = models.TextField(blank=True, default="")  # the manager's reason when sending it back

    class Meta:
        ordering = ["joined_at"]
        constraints = [
            models.UniqueConstraint(F("shift"), F("user"), condition=Q(deleted_at__isnull=True, left_at__isnull=True), name="shiftattendant_one_open_part"),
            models.UniqueConstraint(F("shift"), condition=Q(deleted_at__isnull=True, is_lead=True), name="shiftattendant_one_lead"),
        ]

    @property
    def done(self):
        """Handed in, or marked as left: the shift may close without waiting for this person."""
        return self.cash_submitted_at is not None or self.left_at is not None


class UndoRequest(TenantOwnedModel):
    """An attendant asks the owner to change something they already handed in. Once allowed they may make that change
    once (cash or meter numbers); "ended by mistake" puts them back on the shift as soon as the owner allows it."""

    class Kind(models.TextChoices):
        CASH = "cash", "Change the cash handed in"
        METERS = "meters", "Change the end meter numbers"
        REOPEN = "reopen", "I ended my shift by mistake"

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for the owner"
        ALLOWED = "allowed", "Allowed"
        REFUSED = "refused", "Refused"
        USED = "used", "Done"

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="+")
    attendant = models.ForeignKey(ShiftAttendant, on_delete=models.PROTECT, related_name="undo_requests")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    reason = models.TextField()
    requested_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(F("attendant"), "kind", condition=Q(deleted_at__isnull=True, status__in=["pending", "allowed"]), name="undorequest_one_open_per_kind"),
        ]


class PumpAssignment(TenantOwnedModel):
    """Assigned-pump stations: who holds a pump, from when to when. A swap ends one row and starts the next at a
    meter snapshot, so each person is charged only for the litres sold in their own period."""

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="assignments")
    pump = models.ForeignKey("stations.Pump", on_delete=models.PROTECT, related_name="+")
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    assigned_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["started_at"]
        constraints = [
            models.UniqueConstraint(F("shift"), F("pump"), condition=Q(deleted_at__isnull=True, ended_at__isnull=True), name="pumpassignment_one_open"),
        ]


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
            # One start and one end number per nozzle per PERSON: a second person's different number is kept beside the
            # first and flagged "readings differ", never silently overwritten.
            models.UniqueConstraint(
                F("shift"), F("nozzle"), "kind", F("recorded_by"), condition=Q(deleted_at__isnull=True) & ~Q(kind="checkpoint"), name="reading_unique_per_person"
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
    attendant = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")  # who handed it in
    # The part of the shift it was handed in for: someone who works two parts of one shift hands in twice.
    part = models.ForeignKey(ShiftAttendant, null=True, blank=True, on_delete=models.PROTECT, related_name="cash")
    payment_type = models.CharField(max_length=15, choices=PaymentType.choices)
    currency = models.CharField(max_length=3, validators=[validate_currency])
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(F("part"), "payment_type", "currency", condition=Q(deleted_at__isnull=True, part__isnull=False), name="cash_unique_per_part"),
            models.CheckConstraint(condition=Q(amount__gte=0), name="cash_not_negative"),
        ]


class Transaction(TenantOwnedModel):
    """Litres sold through one nozzle between two meter readings, priced at the price in force when the period began."""

    station = models.ForeignKey("stations.Station", on_delete=models.PROTECT, related_name="+")
    shift = models.ForeignKey(Shift, null=True, on_delete=models.PROTECT, related_name="transactions")
    # Assigned-pump stations only: the attendant who held the pump for this whole period (expected cash per person).
    attendant = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
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


# ---- rollups (rule 9): written when a shift closes or a dip/delivery is saved; dashboards read only these ----------

class DailySalesRollup(TenantOwnedModel):
    station = models.ForeignKey("stations.Station", on_delete=models.PROTECT, related_name="+")
    day = models.DateField()
    fuel_type = models.ForeignKey("stations.FuelType", on_delete=models.PROTECT, related_name="+")
    currency = models.CharField(max_length=3)
    litres = models.DecimalField(max_digits=14, decimal_places=3)
    amount = models.DecimalField(max_digits=16, decimal_places=2)

    class Meta:
        constraints = [
            models.UniqueConstraint(F("station"), "day", F("fuel_type"), "currency", condition=Q(deleted_at__isnull=True), name="dailysales_unique"),
        ]
        indexes = [models.Index(fields=["station", "day"], name="dailysales_station_day")]


class TankStatus(TenantOwnedModel):
    """Latest estimated level, forecast and 7-day unexplained loss for one tank."""

    tank = models.ForeignKey("stations.Tank", on_delete=models.PROTECT, related_name="+")
    data = models.JSONField(default=dict)
    computed_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(F("tank"), condition=Q(deleted_at__isnull=True), name="tankstatus_one_per_tank")]


class ShiftSummary(TenantOwnedModel):
    """A closed shift's totals and cash check (per payment type and per attendant)."""

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="+")
    station = models.ForeignKey("stations.Station", on_delete=models.PROTECT, related_name="+")
    day = models.DateField()
    litres = models.DecimalField(max_digits=14, decimal_places=3)
    flagged = models.BooleanField(default=False)
    cash = models.JSONField(default=dict)
    computed_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(F("shift"), condition=Q(deleted_at__isnull=True), name="shiftsummary_one_per_shift")]
        indexes = [models.Index(fields=["station", "day"], name="shiftsummary_station_day")]
