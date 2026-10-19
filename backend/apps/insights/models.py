from django.db import models
from django.db.models import F, Q

from apps.core.models import TenantOwnedModel


class Alert(TenantOwnedModel):
    class Kind(models.TextChoices):
        LOW_STOCK = "low_stock", "Low stock"
        LOSS = "loss", "Unexplained fuel loss"
        CASH = "cash", "Cash difference"
        MISSED_READING = "missed_reading", "Missed reading"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    station = models.ForeignKey("stations.Station", null=True, on_delete=models.PROTECT, related_name="+")
    tank = models.ForeignKey("stations.Tank", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.WARNING)
    message = models.CharField(max_length=300)
    dedupe_key = models.CharField(max_length=120)
    # open -> resolved (a person, with a reason) | auto_resolved (the cause went away) | awaiting_approval (a manager
    # proposed a write-off: real fuel loss, or an attendant paying back; the owner approves or sends it back)
    status = models.CharField(max_length=20, default="open")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    resolution_reason = models.CharField(max_length=30, blank=True, default="")  # a code from alert_actions.REASONS
    resolution_note = models.TextField(blank=True, default="")
    # the owner has seen and accepted how it was resolved (resolutions by the owner count as reviewed)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reminded_at = models.DateTimeField(null=True, blank=True)  # the station's manager was reminded (open 12 h)
    escalated_at = models.DateTimeField(null=True, blank=True)  # the owner was told (open 24 h)
    reopened_count = models.PositiveSmallIntegerField(default=0)  # came back within 24 h of being closed
    pattern = models.CharField(max_length=200, blank=True, default="")  # a repeat seen across alerts, e.g. same tank 3 times
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(F("tenant"), "dedupe_key", condition=Q(status__in=["open", "awaiting_approval"], deleted_at__isnull=True),
                                    name="alert_one_open_per_key"),
        ]


class AlertEvent(TenantOwnedModel):
    """One step in an alert's story: opened, reminded, escalated, resolved (how), approved, sent back, reviewed,
    reopened, closed by itself. Never edited: the owner reads this to see what was done and by whom."""

    alert = models.ForeignKey(Alert, on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=30)
    actor = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    note = models.TextField(blank=True, default="")
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at"]


class OutboundMessage(TenantOwnedModel):
    """Every SMS/WhatsApp the system sends, so owners can see what went out and why something did not arrive."""

    channel = models.CharField(max_length=12)
    to = models.CharField(max_length=30)
    kind = models.CharField(max_length=30)
    body = models.TextField()
    status = models.CharField(max_length=10)  # sent | failed | skipped
    detail = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
