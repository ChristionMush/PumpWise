from rest_framework import serializers

from .models import Alert, OutboundMessage


class AlertSerializer(serializers.ModelSerializer):
    station_name = serializers.CharField(source="station.name", default=None, read_only=True)

    class Meta:
        model = Alert
        fields = ["id", "kind", "severity", "message", "station", "station_name", "tank", "status", "created_at", "resolved_at"]


class OutboundMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutboundMessage
        fields = ["id", "channel", "to", "kind", "status", "detail", "created_at", "body"]
