from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import Role, User
from .utils import normalize_email, normalize_phone

ASSIGNABLE_ROLES = [Role.MANAGER, Role.ATTENDANT]


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=254)
    password = serializers.CharField(max_length=256, trim_whitespace=False)


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class UserSerializer(serializers.ModelSerializer):
    station_ids = serializers.SerializerMethodField()  # the stations this person works at (empty: all)

    class Meta:
        model = User
        fields = ["id", "tenant_id", "role", "full_name", "email", "phone", "preferred_language", "is_active", "must_change_password", "last_login", "created_at",
                  "station_ids"]
        read_only_fields = fields

    def get_station_ids(self, obj):
        from apps.stations.models import StationAssignment

        return [str(i) for i in StationAssignment.objects.filter(user_id=obj.pk).values_list("station_id", flat=True)]


class _ContactMixin:
    def validate_email(self, value):
        return normalize_email(value)

    def validate_phone(self, value):
        if value in (None, ""):
            return None
        phone = normalize_phone(value)
        if phone is None:
            raise serializers.ValidationError("Enter a valid phone number.")
        return phone


class UserCreateSerializer(_ContactMixin, serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=ASSIGNABLE_ROLES)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    class Meta:
        model = User
        fields = ["full_name", "email", "phone", "role", "password", "preferred_language"]

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate(self, attrs):
        attrs["email"] = attrs.get("email") or None
        attrs["phone"] = attrs.get("phone") or None
        if not attrs["email"] and not attrs["phone"]:
            raise serializers.ValidationError("Provide an email or a phone number.")
        return attrs

    def create(self, validated):
        # tenant comes from the logged-in owner (view passes it); NEVER from the request body.
        return User.objects.create_user(**validated)


class UserUpdateSerializer(_ContactMixin, serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=ASSIGNABLE_ROLES, required=False)
    password = serializers.CharField(write_only=True, required=False, trim_whitespace=False)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    phone = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    class Meta:
        model = User
        fields = ["full_name", "email", "phone", "role", "is_active", "password", "preferred_language"]

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate(self, attrs):
        instance = self.instance
        request = self.context["request"]
        if instance.pk == request.user.pk:
            if "role" in attrs and attrs["role"] != instance.role:
                raise serializers.ValidationError("You cannot change your own role.")
            if attrs.get("is_active") is False:
                raise serializers.ValidationError("You cannot deactivate yourself.")
        email = attrs.get("email", instance.email) or None
        phone = attrs.get("phone", instance.phone) or None
        if not email and not phone:
            raise serializers.ValidationError("A user needs an email or a phone number.")
        return attrs

    def update(self, instance, validated):
        password = validated.pop("password", None)
        for key in ("email", "phone"):
            if key in validated:
                validated[key] = validated[key] or None
        for key, value in validated.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        if password or validated.get("is_active") is False:
            instance.revoke_tokens()
        instance.save()
        return instance


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(trim_whitespace=False)

    def validate_new_password(self, value):
        validate_password(value)
        return value
