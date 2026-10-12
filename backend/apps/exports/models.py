from django.db import models

from apps.core.models import TenantOwnedModel


class TenantExport(TenantOwnedModel):
    """A 'download all my data' request. The zip lives in storage (S3 in production); links to it are time-limited."""

    class Status(models.TextChoices):
        PENDING = "pending", "Preparing"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    kind = models.CharField(max_length=10, default="manual")  # manual | monthly
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    file_key = models.CharField(max_length=300, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    row_counts = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
