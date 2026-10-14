from decimal import Decimal

from rest_framework import serializers

from apps.stations.models import Nozzle, Station, Tank, validate_currency

from .models import CashSubmission, Delivery, PumpMeterReading, Shift, TankReading, Transaction

NUM = {"max_digits": 14, "decimal_places": 2, "min_value": Decimal(0)}


class ReadingInput(serializers.Serializer):
    nozzle = serializers.PrimaryKeyRelatedField(queryset=Nozzle.objects.select_related("pump__tank__fuel_type").all())
    reading = serializers.DecimalField(**NUM)


class CashInput(serializers.Serializer):
    payment_type = serializers.ChoiceField(choices=CashSubmission.PaymentType.choices)
    currency = serializers.CharField(max_length=3, validators=[validate_currency])
    amount = serializers.DecimalField(**NUM)


class ShiftStartInput(serializers.Serializer):
    shift_id = serializers.UUIDField(required=False)
    station = serializers.PrimaryKeyRelatedField(queryset=Station.objects.all())
    started_at = serializers.DateTimeField(required=False)
    readings = ReadingInput(many=True, allow_empty=False)


class ShiftEndInput(serializers.Serializer):
    ended_at = serializers.DateTimeField(required=False)
    readings = ReadingInput(many=True)
    cash = CashInput(many=True, required=False, default=list)


class CheckpointInput(serializers.Serializer):
    nozzle = serializers.PrimaryKeyRelatedField(queryset=Nozzle.objects.select_related("pump__tank__fuel_type").all())
    reading = serializers.DecimalField(**NUM)
    at = serializers.DateTimeField(required=False)


class TankDipInput(serializers.Serializer):
    client_id = serializers.UUIDField(required=False, allow_null=True)
    tank = serializers.PrimaryKeyRelatedField(queryset=Tank.objects.all())
    litres = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal(0), required=False)
    dip_mm = serializers.DecimalField(max_digits=8, decimal_places=1, min_value=Decimal(0), required=False)
    at = serializers.DateTimeField(required=False)
    shift = serializers.PrimaryKeyRelatedField(queryset=Shift.objects.all(), required=False, allow_null=True)


class DeliveryInput(serializers.Serializer):
    client_id = serializers.UUIDField(required=False, allow_null=True)
    tank = serializers.PrimaryKeyRelatedField(queryset=Tank.objects.all())
    litres = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    delivered_at = serializers.DateTimeField(required=False)
    supplier = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    note_number = serializers.CharField(max_length=60, required=False, allow_blank=True, default="")


class ReadingSerializer(serializers.ModelSerializer):
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = PumpMeterReading
        fields = ["id", "nozzle", "kind", "reading", "reading_at", "source", "corrected", "flags", "photo_url"]

    def get_photo_url(self, obj):
        return f"/api/media/pumpmeterreading/{obj.pk}/photo/" if obj.photo else None


class TransactionSerializer(serializers.ModelSerializer):
    fuel_code = serializers.CharField(source="fuel_type.code", read_only=True)

    class Meta:
        model = Transaction
        fields = ["id", "nozzle", "fuel_type", "fuel_code", "litres", "price_per_litre", "currency", "amount", "started_at", "occurred_at", "start_reading", "end_reading"]


class CashSerializer(serializers.ModelSerializer):
    class Meta:
        model = CashSubmission
        fields = ["payment_type", "currency", "amount"]


class ShiftSerializer(serializers.ModelSerializer):
    attendant_name = serializers.CharField(source="attendant.full_name", read_only=True)
    station_name = serializers.CharField(source="station.name", read_only=True)

    class Meta:
        model = Shift
        fields = ["id", "station", "station_name", "attendant", "attendant_name", "started_at", "ended_at", "status", "flags", "notes"]


class TankReadingSerializer(serializers.ModelSerializer):
    recorded_by_name = serializers.CharField(source="recorded_by.full_name", read_only=True, default=None)
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = TankReading
        fields = ["id", "client_id", "tank", "dip_mm", "litres", "reading_at", "source", "recorded_by", "recorded_by_name", "photo_url"]

    def get_photo_url(self, obj):
        return f"/api/media/tankreading/{obj.pk}/photo/" if obj.photo else None


class DeliverySerializer(serializers.ModelSerializer):
    recorded_by_name = serializers.CharField(source="recorded_by.full_name", read_only=True, default=None)
    photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Delivery
        fields = ["id", "client_id", "tank", "delivered_at", "litres_received", "supplier", "note_number", "recorded_by", "recorded_by_name", "photo_url"]

    def get_photo_url(self, obj):
        return f"/api/media/delivery/{obj.pk}/photo/" if obj.photo else None
