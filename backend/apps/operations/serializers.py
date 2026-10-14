from decimal import Decimal

from rest_framework import serializers

from apps.accounts.models import User
from apps.stations.models import Nozzle, Pump, Station, Tank, validate_currency

from .models import CashSubmission, Delivery, PumpAssignment, PumpMeterReading, Shift, ShiftAttendant, TankReading, Transaction

NUM = {"max_digits": 14, "decimal_places": 2, "min_value": Decimal(0)}


class ReadingInput(serializers.Serializer):
    nozzle = serializers.PrimaryKeyRelatedField(queryset=Nozzle.objects.select_related("pump", "tank__fuel_type").all())
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
    # Several-attendants stations: who else works this shift (a manager starting it names everyone; the first is lead).
    attendants = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    # Assigned-pump stations: {pump id: attendant id}. The app pre-fills it from the last shift.
    assignments = serializers.DictField(child=serializers.UUIDField(), required=False, default=dict)

    def validate_attendants(self, ids):
        from apps.accounts.models import User

        users = {str(u.pk): u for u in User.objects.filter(pk__in=ids)}
        if len(users) != len({str(i) for i in ids}):
            raise serializers.ValidationError("Unknown attendant.")
        return [users[str(i)] for i in dict.fromkeys(ids)]


class ShiftEndInput(serializers.Serializer):
    ended_at = serializers.DateTimeField(required=False)
    readings = ReadingInput(many=True)
    cash = CashInput(many=True, required=False, default=list)
    close = serializers.BooleanField(required=False, allow_null=True, default=None)
    user = serializers.UUIDField(required=False, allow_null=True)  # lead/manager handing in for someone


class CashInputOnly(serializers.Serializer):
    cash = CashInput(many=True)
    user = serializers.UUIDField(required=False, allow_null=True)
    at = serializers.DateTimeField(required=False)


class PersonInput(serializers.Serializer):
    user = serializers.UUIDField(required=False, allow_null=True)
    at = serializers.DateTimeField(required=False)


class ReassignInput(serializers.Serializer):
    pump = serializers.PrimaryKeyRelatedField(queryset=Pump.objects.prefetch_related("nozzles").all())
    user = serializers.UUIDField()
    readings = ReadingInput(many=True, allow_empty=False)
    at = serializers.DateTimeField(required=False)


class PumpAssignInput(serializers.Serializer):
    """Manager or owner: who works this pump (null takes it back). Meter numbers are needed only for a swap, when the
    pump is in use on someone's open shift."""

    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.all(), allow_null=True)
    readings = ReadingInput(many=True, required=False, default=list)
    at = serializers.DateTimeField(required=False)


class ResolveInput(serializers.Serializer):
    reading = serializers.PrimaryKeyRelatedField(queryset=PumpMeterReading.objects.all())


class CheckpointInput(serializers.Serializer):
    nozzle = serializers.PrimaryKeyRelatedField(queryset=Nozzle.objects.select_related("pump", "tank__fuel_type").all())
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
    recorded_by_name = serializers.CharField(source="recorded_by.full_name", read_only=True, default=None)

    class Meta:
        model = PumpMeterReading
        fields = ["id", "nozzle", "kind", "reading", "reading_at", "source", "corrected", "flags", "photo_url", "recorded_by", "recorded_by_name"]

    def get_photo_url(self, obj):
        return f"/api/media/pumpmeterreading/{obj.pk}/photo/" if obj.photo else None


class TransactionSerializer(serializers.ModelSerializer):
    fuel_code = serializers.CharField(source="fuel_type.code", read_only=True)

    class Meta:
        model = Transaction
        fields = ["id", "nozzle", "fuel_type", "fuel_code", "litres", "price_per_litre", "currency", "amount", "started_at", "occurred_at", "start_reading", "end_reading", "attendant"]


class CashSerializer(serializers.ModelSerializer):
    attendant_name = serializers.CharField(source="attendant.full_name", read_only=True, default=None)

    class Meta:
        model = CashSubmission
        fields = ["payment_type", "currency", "amount", "attendant", "attendant_name"]


class ShiftAttendantSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = ShiftAttendant
        fields = ["user", "name", "is_lead", "joined_at", "left_at", "cash_submitted_at"]


class PumpAssignmentSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.full_name", read_only=True)
    pump_name = serializers.CharField(source="pump.name", read_only=True)

    class Meta:
        model = PumpAssignment
        fields = ["pump", "pump_name", "user", "name", "started_at", "ended_at"]


class ShiftSerializer(serializers.ModelSerializer):
    station_name = serializers.CharField(source="station.name", read_only=True)
    people = serializers.SerializerMethodField()
    lead_name = serializers.SerializerMethodField()

    class Meta:
        model = Shift
        fields = ["id", "station", "station_name", "started_at", "ended_at", "status", "flags", "notes", "close_requested_at", "people", "lead_name"]

    def _rows(self, obj):
        return list(obj.attendants.all())  # prefetched by the list view

    def get_people(self, obj):
        return [a.user.full_name for a in self._rows(obj)]

    def get_lead_name(self, obj):
        return next((a.user.full_name for a in self._rows(obj) if a.is_lead), None)


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
