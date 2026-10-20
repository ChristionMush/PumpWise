from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from .models import CalibrationPoint, ExchangeRate, FuelPrice, FuelType, Nozzle, Pump, Station, Tank, pump_nozzle_problem, validate_currency


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
        fields = ["id", "station", "name", "fuel_type", "fuel_code", "capacity_litres", "reserve_litres", "data_source", "has_calibration"]
        read_only_fields = ["id"]

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
    """A pump's nozzle. More can be added to a pump, and one can be removed or moved to another tank, as long as the
    pump keeps at least one nozzle."""

    pump = serializers.PrimaryKeyRelatedField(queryset=Pump.objects.all())
    tank = serializers.PrimaryKeyRelatedField(queryset=Tank.objects.all())
    tank_name = serializers.CharField(source="tank.name", read_only=True)
    fuel_type = serializers.CharField(source="tank.fuel_type_id", read_only=True)
    fuel_code = serializers.CharField(source="tank.fuel_type.code", read_only=True)

    class Meta:
        model = Nozzle
        fields = ["id", "pump", "number", "label", "meter_max", "tank", "tank_name", "fuel_type", "fuel_code"]
        read_only_fields = ["id", "number"]

    def validate(self, attrs):
        nozzle = self.instance
        pump = nozzle.pump if nozzle is not None else attrs["pump"]
        if nozzle is not None and "pump" in attrs and attrs["pump"].pk != nozzle.pump_id:
            raise serializers.ValidationError({"pump": "A nozzle cannot move to another pump."})
        tank = attrs.get("tank")
        if tank is not None:
            if tank.station_id != pump.station_id:
                raise serializers.ValidationError({"tank": "Choose a tank at this pump's station."})
            if nozzle is not None:
                tanks = [n.tank_id if n.pk != nozzle.pk else tank.pk for n in pump.nozzles.all()]
                if problem := pump_nozzle_problem(tanks):
                    raise serializers.ValidationError({"tank": problem})
        return attrs

    def create(self, validated_data):
        last = Nozzle.objects.filter(pump=validated_data["pump"]).order_by("-number").first()
        validated_data["number"] = (last.number if last else 0) + 1
        if not validated_data.get("label"):
            validated_data["label"] = validated_data["tank"].fuel_type.name
        return super().create(validated_data)


class NewNozzleSerializer(serializers.Serializer):
    tank = serializers.PrimaryKeyRelatedField(queryset=Tank.objects.all())
    label = serializers.CharField(max_length=40, required=False, allow_blank=True, default="")
    meter_max = serializers.DecimalField(max_digits=14, decimal_places=2, required=False)


class PumpSerializer(serializers.ModelSerializer):
    """A pump starts with at least one nozzle, each drawing from a tank at the pump's station; more can be added
    later. They are sent with the pump when it is created: {"nozzles": [{"tank": <id>, "label": "Petrol"}, ...]}."""

    station = serializers.PrimaryKeyRelatedField(queryset=Station.objects.all())
    nozzles = NozzleSerializer(many=True, read_only=True)
    new_nozzles = NewNozzleSerializer(many=True, write_only=True, required=False, source="nozzle_input")
    assigned_to = serializers.PrimaryKeyRelatedField(read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    nameplate_photo_url = serializers.SerializerMethodField()
    display_photo_url = serializers.SerializerMethodField()

    class Meta:
        model = Pump
        fields = ["id", "station", "name", "brand", "model", "year", "data_source", "nozzles", "new_nozzles", "assigned_to", "assigned_to_name",
                  "nameplate_photo_url", "display_photo_url"]
        read_only_fields = ["id"]

    def to_internal_value(self, data):
        # The app sends "nozzles" when creating a pump; it is the same list as new_nozzles.
        if hasattr(data, "get") and "nozzles" in data and "new_nozzles" not in data:
            data = {**data, "new_nozzles": data["nozzles"]}
        return super().to_internal_value(data)

    def get_assigned_to_name(self, obj):
        return obj.assigned_to.full_name if obj.assigned_to_id else None

    def get_nameplate_photo_url(self, obj):
        return file_url(obj, "nameplate_photo")

    def get_display_photo_url(self, obj):
        return file_url(obj, "display_photo")

    def validate(self, attrs):
        station = attrs.get("station", getattr(self.instance, "station", None))
        if self.instance is not None and "station" in attrs and attrs["station"].pk != self.instance.station_id:
            raise serializers.ValidationError({"station": "A pump cannot move to another station."})
        year = attrs.get("year")
        if year and not (1950 <= year <= timezone.now().year + 1):
            raise serializers.ValidationError({"year": "Enter a valid year."})
        if self.instance is None:
            tanks = [n["tank"] for n in attrs.get("nozzle_input") or []]
            if any(t.station_id != station.pk for t in tanks):
                raise serializers.ValidationError({"nozzles": "Every nozzle's tank must be at the pump's station."})
            if problem := pump_nozzle_problem(t.pk for t in tanks):
                raise serializers.ValidationError({"nozzles": problem + " Choose the tank each nozzle draws from."})
        elif "nozzle_input" in attrs:
            raise serializers.ValidationError({"nozzles": "Add, change or remove nozzles one at a time."})
        return attrs

    def create(self, validated_data):
        nozzles = validated_data.pop("nozzle_input")
        pump = super().create(validated_data)
        for number, n in enumerate(nozzles, start=1):
            extra = {"meter_max": n["meter_max"]} if n.get("meter_max") is not None else {}
            Nozzle.objects.create(tenant_id=pump.tenant_id, pump=pump, tank=n["tank"], number=number, label=n.get("label") or n["tank"].fuel_type.name, **extra)
        return pump


class FuelPriceSerializer(serializers.ModelSerializer):
    fuel_code = serializers.CharField(source="fuel_type.code", read_only=True)

    class Meta:
        model = FuelPrice
        fields = ["id", "station", "fuel_type", "fuel_code", "currency", "price_per_litre", "effective_from", "effective_to", "created_at"]
        read_only_fields = fields


class FuelPriceOwnerSerializer(FuelPriceSerializer):
    """The owner also sees who set each price; created_at is when they set it."""

    set_by = serializers.CharField(source="created_by.full_name", default=None, read_only=True)

    class Meta(FuelPriceSerializer.Meta):
        fields = [*FuelPriceSerializer.Meta.fields, "set_by"]
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
