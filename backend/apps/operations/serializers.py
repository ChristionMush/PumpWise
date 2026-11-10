from decimal import Decimal

from rest_framework import serializers

from apps.accounts.models import User
from apps.stations.models import Nozzle, Pump, Station, Tank, validate_currency

from .models import CashSubmission, Delivery, PumpAssignment, PumpMeterReading, Shift, ShiftAttendant, TankReading, Transaction

NUM = {"max_digits": 14, "decimal_places": 2, "min_value": Decimal(0)}


class ReadingInput(serializers.Serializer):
    nozzle = serializers.PrimaryKeyRelatedField(queryset=Nozzle.objects.select_related("pump", "tank__fuel_type").all())
    reading = serializers.DecimalField(**NUM)
    # taking a pump over: the number the attendant found on the pump, when it is not the handover number (kept for the manager)
    seen = serializers.DecimalField(**NUM, required=False, allow_null=True)


class CashInput(serializers.Serializer):
    payment_type = serializers.ChoiceField(choices=CashSubmission.PaymentType.choices)
    currency = serializers.CharField(max_length=3, validators=[validate_currency])
    amount = serializers.DecimalField(**NUM)
    # intercompany issue: who was credited. Asked for on the phone; never required here, so an older phone's
    # queued hand-in is not refused (the accounting pack shows it without a name)
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        attrs["note"] = " ".join(attrs.get("note", "").split())
        # notes are counted in whole dollars; swipe, EcoCash and intercompany credit can have cents
        if attrs["payment_type"] == CashSubmission.PaymentType.CASH and attrs["amount"] != attrs["amount"].to_integral_value():
            raise serializers.ValidationError({"amount": "Cash is counted in whole dollars. Enter the cash you counted, without cents."})
        return attrs


class ShiftExpenseInput(serializers.Serializer):
    """An expense an attendant paid on their shift, sent with their end of shift or cash: what it was, and what it cost."""

    client_id = serializers.UUIDField(required=False, allow_null=True)  # made on the phone: a resend never records it twice
    name = serializers.CharField(max_length=200)
    amount = serializers.DecimalField(**NUM)

    def validate(self, attrs):
        attrs["name"] = " ".join(attrs["name"].split())
        if not attrs["name"]:
            raise serializers.ValidationError({"name": "Say what the expense was."})
        if attrs["amount"] <= 0:
            raise serializers.ValidationError({"amount": f"Enter what {attrs['name']} cost."})
        return attrs


class ExpenseInput(serializers.Serializer):
    client_id = serializers.UUIDField(required=False, allow_null=True)
    station = serializers.PrimaryKeyRelatedField(queryset=Station.objects.all())
    category = serializers.ChoiceField(choices=__import__("apps.operations.models", fromlist=["Expense"]).Expense.Category.choices)
    amount = serializers.DecimalField(**NUM)
    currency = serializers.CharField(max_length=3, required=False, validators=[validate_currency])
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    spent_at = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        if attrs["amount"] <= 0:
            raise serializers.ValidationError({"amount": "Enter what was paid."})
        if attrs["category"] == "other" and not attrs.get("note", "").strip():
            raise serializers.ValidationError({"note": "Say what the money was paid for."})
        attrs["note"] = " ".join(attrs.get("note", "").split())
        return attrs


class ExpenseSerializer(serializers.ModelSerializer):
    recorded_by_name = serializers.CharField(source="recorded_by.full_name", read_only=True)
    station_name = serializers.CharField(source="station.name", read_only=True)
    category_label = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = __import__("apps.operations.models", fromlist=["Expense"]).Expense
        fields = ["id", "station", "station_name", "shift", "category", "category_label", "amount", "currency", "note", "spent_at", "recorded_by", "recorded_by_name"]


class ShiftStartInput(serializers.Serializer):
    shift_id = serializers.UUIDField(required=False)
    station = serializers.PrimaryKeyRelatedField(queryset=Station.objects.all())
    started_at = serializers.DateTimeField(required=False)
    readings = ReadingInput(many=True, allow_empty=False)
    # Several-attendants stations: who else works this shift (a manager starting it names everyone; the first is lead).
    attendants = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    # Assigned-pump stations: {pump id: attendant id}. The app pre-fills it from the last shift.
    assignments = serializers.DictField(child=serializers.UUIDField(), required=False, default=dict)
    # A business where attendants pick their pumps: the pumps this attendant takes
    pumps = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)

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
    cash_later = serializers.BooleanField(required=False, default=False)  # assigned pumps: close the meters now, cash after
    dips = serializers.ListField(child=serializers.DictField(), required=False, default=list)  # dip at the end of a shift (checked in services)
    expenses = serializers.ListField(child=serializers.DictField(), required=False, default=list)  # paid out on the shift (ShiftExpenseInput)


class CashInputOnly(serializers.Serializer):
    cash = CashInput(many=True)
    expenses = serializers.ListField(child=serializers.DictField(), required=False, default=list)  # paid out on the shift (ShiftExpenseInput)
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
    fuel_code = serializers.CharField(max_length=20, required=False, allow_blank=True)  # the fuel the manager says is arriving

    def validate(self, attrs):
        fuel = (attrs.get("fuel_code") or "").strip().upper()
        if fuel and attrs["tank"].fuel_type.code.upper() != fuel:
            raise serializers.ValidationError({"tank": f"{attrs['tank'].name} holds {attrs['tank'].fuel_type.name}, not {fuel}. Choose a {fuel} tank."})
        return attrs


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
        fields = ["payment_type", "currency", "amount", "note", "attendant", "attendant_name"]


class ShiftAttendantSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.full_name", read_only=True)

    class Meta:
        model = ShiftAttendant
        fields = ["id", "user", "name", "is_lead", "joined_at", "left_at", "cash_submitted_at", "approval", "approval_note"]


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
        return list(dict.fromkeys(a.user.full_name for a in self._rows(obj)))  # once each, even with two parts

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


class DayCloseDipInput(serializers.Serializer):
    tank = serializers.PrimaryKeyRelatedField(queryset=Tank.objects.all())
    litres = serializers.DecimalField(max_digits=12, decimal_places=3, required=False, allow_null=True)
    dip_mm = serializers.DecimalField(max_digits=8, decimal_places=1, required=False, allow_null=True)

    def validate(self, attrs):
        # the dip height in mm is always taken first; the litres too, unless the tank's chart gives them
        if attrs.get("dip_mm") is None:
            raise serializers.ValidationError(f"{attrs['tank'].name}: enter the dip height in mm first.")
        return attrs


class DayCloseMeterInput(serializers.Serializer):
    nozzle = serializers.PrimaryKeyRelatedField(queryset=Nozzle.objects.select_related("pump"))
    reading = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"))


class DayCloseCorrectInput(serializers.Serializer):
    """A correction to the latest day close: the dips and meters to change (each tank's dip in mm, then litres)."""

    dips = DayCloseDipInput(many=True, required=False, default=list)
    meters = DayCloseMeterInput(many=True, required=False, default=list)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class DayCloseInput(serializers.Serializer):
    """The manager's day close at about 21:00: every tank's dip and every pump's meter at the station."""

    station = serializers.PrimaryKeyRelatedField(queryset=Station.objects.all())
    at = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    dips = DayCloseDipInput(many=True)
    meters = DayCloseMeterInput(many=True)
