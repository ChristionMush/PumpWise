from django.conf import settings
from rest_framework import serializers

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    """What a tenant owner sees. Platform staff identities are shown as the company, never by name."""

    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ["id", "created_at", "action", "actor_name", "via_platform", "object_type", "object_id", "changes", "ip_address"]
        read_only_fields = fields

    def get_actor_name(self, obj):
        if obj.via_platform:
            return settings.BUILT_BY
        return obj.actor.full_name if obj.actor else None


class PlatformAuditSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.full_name", default=None, read_only=True)
    tenant_name = serializers.CharField(source="tenant.name", default=None, read_only=True)

    class Meta:
        model = AuditLog
        fields = ["id", "created_at", "action", "actor_name", "tenant_id", "tenant_name", "object_type", "object_id", "changes", "ip_address"]
        read_only_fields = fields
