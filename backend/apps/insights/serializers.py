from rest_framework import serializers

from .alert_actions import reason_label
from .models import Alert, AlertEvent, OutboundMessage


class AlertSerializer(serializers.ModelSerializer):
    station_name = serializers.CharField(source="station.name", default=None, read_only=True)
    resolved_by_name = serializers.CharField(source="resolved_by.full_name", default=None, read_only=True)
    resolved_by_role = serializers.CharField(source="resolved_by.role", default=None, read_only=True)
    reviewed_by_name = serializers.CharField(source="reviewed_by.full_name", default=None, read_only=True)
    resolution_label = serializers.SerializerMethodField()

    class Meta:
        model = Alert
        fields = ["id", "kind", "severity", "message", "station", "station_name", "tank", "status", "created_at", "resolved_at",
                  "resolution_reason", "resolution_label", "resolution_note", "resolved_by_name", "resolved_by_role",
                  "reviewed_at", "reviewed_by_name", "reminded_at", "escalated_at", "reopened_count", "pattern"]

    def get_resolution_label(self, obj):
        return reason_label(obj.kind, obj.resolution_reason) if obj.resolution_reason else None


class AlertEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.full_name", default=None, read_only=True)
    actor_role = serializers.CharField(source="actor.role", default=None, read_only=True)

    class Meta:
        model = AlertEvent
        fields = ["id", "kind", "actor_name", "actor_role", "note", "data", "created_at"]


class OutboundMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutboundMessage
        fields = ["id", "channel", "to", "kind", "status", "detail", "created_at", "body"]
