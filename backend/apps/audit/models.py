import uuid

from django.db import models


class AuditLog(models.Model):
    """Who changed what, when. Append-only: the app DB role has no UPDATE/DELETE grant on
    this table, and the model refuses to update or delete as well."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenants.Tenant", null=True, on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey("accounts.User", null=True, on_delete=models.PROTECT, related_name="+")
    action = models.CharField(max_length=40, db_index=True)
    object_type = models.CharField(max_length=60, blank=True, default="")
    object_id = models.CharField(max_length=64, blank=True, default="")
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    # True when Tendeka & Elichi staff acted inside a tenant; the tenant can see that, and so can the platform.
    via_platform = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("AuditLog is append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("AuditLog is append-only")
