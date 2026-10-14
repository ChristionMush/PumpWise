from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from .models import CalibrationPoint, ExchangeRate, FuelPrice, FuelType, Nozzle, Pump, Station, Tank, validate_currency


def file_url(obj, field):
    return f"/api/media/{obj._meta.model_name}/{obj.pk}/{field}/" if getattr(obj, field) else None


class StationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Station
        fields = ["id", "name", "address", "status", "pricing_currency", "attendants_mode", "assignment_mode", "created_at"]
        # The attendant policy is set by the platform only (apps/tenants/features.py), never through this API.
        read_only_fields = ["id", "attendants_mode", "assignment_mode", "created_at"]


class StationInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")

    def validate_name(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("Enter a station name.")
        return value


class FuelTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelType
        fields = ["id", "name", "code"]
        read_only_fields = ["id"]

    def validate_code(self, value):
        return value.strip().upper()


class CalibrationPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = CalibrationPoint
        fields = ["dip_mm", "litres"]


class TankSerializer(serializers.ModelSerializer):
    fuel_type = serializers.PrimaryKeyRelatedField(queryset=FuelType.objects.all())
    station = serializers.PrimaryKeyRelatedField(queryset=Station.objects.all())
    fuel_code = serializers.CharField(source="fuel_type.code", read_only=True)
    has_calibration = serializers.SerializerMethodField()

    class Meta:
        model = Tank
        fields = ["id", "station", "name", "fuel_type", "fuel_code", "capacity_litres", "reserve_litres", "data_source", "qr_token", "has_calibration"]
        read_only_fields = ["id", "qr_token"]

    def get_has_calibration(self, obj):
        return obj.calibration.count() >= 2

    def validate(self, attrs):
        cap = attrs.get("capacity_litres", getattr(self.instance, "capacity_litres", None))
        res = attrs.get("reserve_litres", getattr(self.instance, "reserve_litres", Decimal(0)))
        if cap is not None and res > cap:
            raise serializers.ValidationError("Reserve cannot be more than the tank capacity.")
        return attrs


class CalibrationInputSerializer(serializers.Serializer):
    points = CalibrationPointSerializer(many=True)

    def validate_points(self, pts):
        if len(pts) < 2:
            raise serializers.ValidationError("Enter at least two rows (dip height and litres).")
        dips = [p["dip_mm"] for p in pts]
        if len(set(dips)) != len(dips):
            raise serializers.ValidationError("Two rows have the same dip height.")
        ordered = sorted(pts, key=lambda p: p["dip_mm"])
        if any(b["litres"] < a["litres"] for a, b in zip(ordered, ordered[1:], strict=False)):
            raise serializers.ValidationError("Litres must go up as the dip height goes up.")
        return ordered


class NozzleSerializer(serializers.ModelSerializer):
    pump = serializers.PrimaryKeyRelatedField(queryset=Pump.objects.all())

    class Meta:
        model = Nozzle
        fields = ["id", "pump", "number", "label", "meter_max"]
        read_only_fields = ["id"]


class PumpSerializer(serializers.ModelSerializer):
    station = serializers.PrimaryKeyRelatedField(queryset=Station.objects.all())
    tank = serializers.PrimaryKeyRelatedField(queryset=Tank.objects.all())
    nozzles = NozzleSerializer(many=True, read_only=True)
    nameplate_photo_url = serializers.SerializerMethodField()
    display_photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Pump
        fields = ["id", "station", "tank", "name", "brand", "model", "year", "data_source", "qr_token", "nozzles", "nameplate_photo_url", "display_photo_url"]
        read_only_fields = ["id", "qr_token"]

    def get_nameplate_photo_url(self, obj):
        return file_url(obj, "nameplate_photo")

    def get_display_photo_url(self, obj):
        return file_url(obj, "display_photo")

    def validate(self, attrs):
        station = attrs.get("station", getattr(self.instance, "station", None))
        tank = attrs.get("tank", getattr(self.instance, "tank", None))
        if station and tank and tank.station_id != station.pk:
            raise serializers.ValidationError("The tank must be at the same station as the pump.")
        year = attrs.get("year")
        if year and not (1950 <= year <= timezone.now().year + 1):
            raise serializers.ValidationError({"year": "Enter a valid year."})
        return attrs


class FuelPriceSerializer(serializers.ModelSerializer):
    fuel_code = serializers.CharField(source="fuel_type.code", read_only=True)

    class Meta:
        model = FuelPrice
        fields = ["id", "station", "fuel_type", "fuel_code", "currency", "price_per_litre", "effective_from", "effective_to", "created_at"]
        read_only_fields = fields


class PriceInputSerializer(serializers.Serializer):
    station = serializers.PrimaryKeyRelatedField(queryset=Station.objects.all())
    fuel_type = serializers.PrimaryKeyRelatedField(queryset=FuelType.objects.all())
    currency = serializers.CharField(max_length=3, validators=[validate_currency], required=False)
    price_per_litre = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=Decimal("0.0001"))
    effective_from = serializers.DateTimeField(required=False)


class ExchangeRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExchangeRate
        fields = ["id", "from_currency", "to_currency", "rate", "effective_from"]
        read_only_fields = ["id"]
        extra_kwargs = {"effective_from": {"required": False}}

    def validate(self, attrs):
        if attrs["from_currency"] == attrs["to_currency"]:
            raise serializers.ValidationError("Choose two different currencies.")
        return attrs
