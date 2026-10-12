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
    status = models.CharField(max_length=10, default="open")  # open | resolved
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(F("tenant"), "dedupe_key", condition=Q(status="open", deleted_at__isnull=True), name="alert_one_open_per_key"),
        ]


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
