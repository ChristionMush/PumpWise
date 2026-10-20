from rest_framework import serializers

from apps.accounts.utils import normalize_email, normalize_phone
from apps.stations.serializers import StationInputSerializer

from .models import Subscription, Tenant


class SubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subscription
        fields = ["status", "plan", "paid_through"]


class SubscriptionUpdateSerializer(serializers.ModelSerializer):
    reseller = serializers.PrimaryKeyRelatedField(queryset=__import__("apps.billing.models", fromlist=["Reseller"]).Reseller.objects.all(), required=False, allow_null=True)

    class Meta:
        model = Subscription
        fields = ["status", "plan", "paid_through", "reseller", "addons"]
        extra_kwargs = {"plan": {"required": False}, "status": {"required": False}, "paid_through": {"required": False}, "addons": {"required": False}}


class TenantUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    status = serializers.ChoiceField(choices=Tenant.Status.choices, required=False)

    def validate_name(self, value):
        value = " ".join(value.split())
        if len(value) < 2:
            raise serializers.ValidationError("Enter a business name.")
        return value


class TenantCreateSerializer(serializers.Serializer):
    business_name = serializers.CharField(max_length=200)
    owner_name = serializers.CharField(max_length=200)
    owner_email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    owner_phone = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    plan = serializers.CharField(max_length=60, required=False, allow_blank=True, default="")
    paid_through = serializers.DateField(required=False, allow_null=True, default=None)
    stations = StationInputSerializer(many=True, required=False, default=list)
    # Asked on the platform's "New business" form: shared pumps, or each attendant works their own assigned pumps.
    pump_system = serializers.ChoiceField(choices=["shared", "shared_multi", "assigned"], required=False, default="shared")

    def validate_business_name(self, value):
        value = " ".join(value.split())
        if len(value) < 2:
            raise serializers.ValidationError("Enter a business name.")
        return value

    def validate_owner_email(self, value):
        return normalize_email(value)

    def validate_owner_phone(self, value):
        if not value:
            return None
        phone = normalize_phone(value)
        if phone is None:
            raise serializers.ValidationError("Enter a valid phone number.")
        return phone

    def validate_stations(self, value):
        if len(value) > 20:
            raise serializers.ValidationError("Add at most 20 stations at once; add more afterwards.")
        names = [s["name"].lower() for s in value]
        if len(names) != len(set(names)):
            raise serializers.ValidationError("Two stations have the same name.")
        return value

    def validate(self, attrs):
        if not attrs.get("owner_email") and not attrs.get("owner_phone"):
            raise serializers.ValidationError("Give the owner an email or a phone number.")
        return attrs


class TenantSettingsSerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = __import__("apps.tenants.models", fromlist=["TenantSettings"]).TenantSettings
        fields = ["display_name", "logo_url", "loss_threshold_litres", "loss_threshold_pct", "cash_variance_amount", "cash_variance_pct",
                  "low_stock_days", "refill_lead_days", "summary_channel", "summary_phone"]

    def get_logo_url(self, obj):
        return f"/api/media/tenantsettings/{obj.pk}/logo/" if obj.logo else None

    def validate_summary_phone(self, value):
        from apps.accounts.utils import normalize_phone

        if not value:
            return ""
        phone = normalize_phone(value)
        if phone is None:
            raise serializers.ValidationError("Enter a valid phone number.")
        return phone


class FeaturesSerializer(serializers.Serializer):
    multi_attendant_enabled = serializers.BooleanField(required=False)
    pump_assignment_enabled = serializers.BooleanField(required=False)


class PumpSystemSerializer(serializers.Serializer):
    pump_system = serializers.ChoiceField(choices=["shared", "shared_multi", "assigned"])


class StationPolicySerializer(serializers.Serializer):
    attendants_mode = serializers.ChoiceField(choices=["single", "multiple"], required=False)
    assignment_mode = serializers.ChoiceField(choices=["shared", "assigned"], required=False)


class TrialCreateSerializer(serializers.Serializer):
    prospect_name = serializers.CharField(max_length=200)
    owner_email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    owner_phone = serializers.CharField(max_length=20, required=False, allow_null=True, allow_blank=True)

    def validate_prospect_name(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("Enter the prospect's business name.")
        return value

    def validate(self, attrs):
        attrs["owner_email"] = normalize_email(attrs.get("owner_email")) or None
        attrs["owner_phone"] = normalize_phone(attrs.get("owner_phone")) or None
        return attrs
