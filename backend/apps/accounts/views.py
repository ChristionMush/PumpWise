from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.exceptions import (
    AuthenticationFailed,
    PermissionDenied,
    ValidationError,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit import services as audit
from apps.core.db import clear_db_context, set_db_context

from .authentication import FAILED, set_context_for_user, user_from_claims
from .models import Role, User
from .permissions import OwnerWriteManagerRead
from .serializers import (
    ChangePasswordSerializer,
    LoginSerializer,
    RefreshSerializer,
    UserCreateSerializer,
    UserSerializer,
    UserUpdateSerializer,
)
from .tokens import TokenError, decode_token, issue_tokens
from .utils import normalize_identifier

# Verifying a password for an unknown user costs the same as for a known one (no timing oracle).
_DUMMY_HASH = make_password("not-a-real-password")
AUDITED_USER_FIELDS = ("full_name", "email", "phone", "role", "is_active", "preferred_language")


def _branding():
    return {"built_by": settings.BUILT_BY}


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        data = LoginSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        identifier = normalize_identifier(data.validated_data["identifier"])
        password = data.validated_data["password"]

        user = None
        if identifier:
            # Only this one identifier becomes visible through RLS, only during this lookup.
            set_db_context(login_identifier=identifier)
            qs = User.objects.filter(email__iexact=identifier) if "@" in identifier else User.objects.filter(phone=identifier)
            user = qs.first()

        password_ok = check_password(password, user.password if user else _DUMMY_HASH)
        if user is None or not password_ok or not user.is_active:
            if user is not None:
                set_context_for_user(user)
                audit.log("login_failed", actor=user, obj=user, request=request)
            else:
                clear_db_context()
            # Return (not raise) so the transaction commits and the audit row is kept.
            return Response({"detail": "Wrong login details."}, status=status.HTTP_401_UNAUTHORIZED)

        set_context_for_user(user)
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        audit.log("login", actor=user, obj=user, request=request)
        return Response({**issue_tokens(user), "user": UserSerializer(user).data, "branding": _branding()})


class RefreshView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "refresh"

    def post(self, request):
        data = RefreshSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            user = user_from_claims(decode_token(data.validated_data["refresh"], "refresh"))
        except (TokenError, AuthenticationFailed):
            return Response({"detail": FAILED}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(issue_tokens(user))


class LogoutView(APIView):
    def post(self, request):
        user = request.user
        user.revoke_tokens()
        user.save(update_fields=["token_version", "updated_at"])
        audit.log("logout", actor=user, obj=user, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        user = request.user
        tenant = None
        if user.tenant_id:
            t = user.tenant
            tenant = {"id": str(t.id), "name": t.name, "slug": t.slug, "status": t.status}
        branding = _branding()
        if user.tenant_id:
            from apps.tenants.models import TenantSettings

            ts = TenantSettings.objects.filter(tenant_id=user.tenant_id).first()
            if ts:
                branding["display_name"] = ts.display_name or None
                branding["logo_url"] = f"/api/media/tenantsettings/{ts.pk}/logo/" if ts.logo else None
        billing = None
        if user.tenant_id and user.role in ("owner", "manager"):
            from apps.billing.services import banner_for
            from apps.tenants.models import Subscription

            billing = banner_for(Subscription.objects.filter(tenant_id=user.tenant_id).first(), user.tenant)
        return Response({"user": UserSerializer(user).data, "tenant": tenant, "branding": branding, "billing": billing})


class ChangePasswordView(APIView):
    def post(self, request):
        data = ChangePasswordSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(data.validated_data["current_password"]):
            raise ValidationError({"current_password": "Wrong password."})
        user.set_password(data.validated_data["new_password"])
        user.must_change_password = False
        user.revoke_tokens()
        user.save(update_fields=["password", "must_change_password", "token_version", "updated_at"])
        audit.log("password_changed", actor=user, obj=user, request=request)
        return Response(issue_tokens(user))  # keeps this device signed in; all others are signed out


class UserViewSet(viewsets.ModelViewSet):
    """Users of the caller's tenant.

    There is deliberately NO tenant filter in the queryset: Row-Level Security in the
    database is what scopes it, and tests/test_isolation_api.py proves that is enough.
    """

    permission_classes = [OwnerWriteManagerRead]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = User.objects.all()

    def get_serializer_class(self):
        return {"create": UserCreateSerializer, "partial_update": UserUpdateSerializer}.get(self.action, UserSerializer)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():  # savepoint: a duplicate must not poison the request transaction
                user = serializer.save(tenant_id=request.user.tenant_id)
        except IntegrityError:
            raise ValidationError({"detail": "That email or phone number is already in use."})
        audit.log("user_created", actor=request.user, obj=user, changes=audit.snapshot(user, AUDITED_USER_FIELDS), request=request)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        user = self.get_object()
        self._guard_target(user)
        before = audit.snapshot(user, AUDITED_USER_FIELDS)
        serializer = self.get_serializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                user = serializer.save()
        except IntegrityError:
            raise ValidationError({"detail": "That email or phone number is already in use."})
        audit.log("user_updated", actor=request.user, obj=user, changes=audit.diff(before, audit.snapshot(user, AUDITED_USER_FIELDS)), request=request)
        if "password" in request.data:
            audit.log("password_reset", actor=request.user, obj=user, request=request)
        return Response(UserSerializer(user).data)

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        if user.pk == request.user.pk:
            raise ValidationError({"detail": "You cannot delete yourself."})
        self._guard_target(user)
        user.deactivate()  # soft delete; the row and its history stay
        audit.log("user_deleted", actor=request.user, obj=user, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _guard_target(self, target):
        # Owners manage managers and attendants. Other owners are only changeable by the platform,
        # which also guarantees a tenant can never lose its last owner through this API.
        if target.role == Role.OWNER and target.pk != self.request.user.pk:
            raise PermissionDenied("Owners cannot modify other owners.")
