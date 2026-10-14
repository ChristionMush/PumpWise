"""Platform admin API (Tendeka & Elichi staff only).

Platform staff never get a blanket bypass. To touch tenant-owned data (the owner login,
stations) these views narrow the database context to exactly ONE tenant with `tenant_scope`,
and every such action is written to the audit log as `via_platform`.
"""
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Role, User
from apps.accounts.permissions import IsOwnerOrManager, IsPlatformAdmin, require
from apps.accounts.utils import generate_temporary_password
from apps.audit import services as audit
from apps.core.db import tenant_scope
from apps.stations.models import Station
from apps.stations.serializers import StationInputSerializer, StationSerializer

from .models import Subscription, Tenant
from .serializers import (
    SubscriptionSerializer,
    SubscriptionUpdateSerializer,
    TenantCreateSerializer,
    TenantUpdateSerializer,
)
from .services import create_tenant_with_owner

DUPLICATE = "That email or phone number is already in use."


class TenantPagination(LimitOffsetPagination):
    # Each row needs a few queries inside its own tenant scope, so keep pages small.
    default_limit = 25
    max_limit = 50


def _get_tenant(pk):
    try:
        tenant = Tenant.objects.select_related("subscription").filter(pk=pk).first()
    except Exception:  # malformed UUID
        tenant = None
    if tenant is None:
        raise NotFound("Tenant not found.")
    return tenant


def _owner_dict(owner):
    if owner is None:
        return None
    return {"id": str(owner.id), "full_name": owner.full_name, "email": owner.email, "phone": owner.phone}


def summarize(tenant, *, with_stations=False):
    """Tenant + subscription (platform can read) + owner/stations (read inside this one tenant's scope)."""
    sub = getattr(tenant, "subscription", None)
    with tenant_scope(tenant.id):
        owner = User.objects.filter(role=Role.OWNER).order_by("created_at").first()
        stations = list(Station.objects.all())
    data = {
        "id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
        "status": tenant.status,
        "created_at": tenant.created_at,
        "subscription": SubscriptionSerializer(sub).data if sub else None,
        "station_count": len(stations),
        "owner": _owner_dict(owner),
    }
    if with_stations:
        data["stations"] = StationSerializer(stations, many=True).data
    return data


class PlatformTenantListCreateView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        qs = Tenant.objects.select_related("subscription").order_by("name")
        wanted = request.query_params.get("status")
        if wanted in Tenant.Status.values:
            qs = qs.filter(status=wanted)
        sub_status = request.query_params.get("subscription")
        if sub_status in Subscription.Status.values:
            qs = qs.filter(subscription__status=sub_status)
        q = (request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(name__icontains=q)
        paginator = TenantPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response([summarize(t) for t in page])

    def post(self, request):
        """Create tenant + subscription + owner login + first stations in ONE atomic step."""
        data = TenantCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        try:
            tenant, owner, temporary_password = create_tenant_with_owner(
                business_name=d["business_name"], owner_name=d["owner_name"], owner_email=d.get("owner_email"), owner_phone=d.get("owner_phone"),
                plan=d["plan"], paid_through=d["paid_through"], stations=d["stations"], actor=request.user, request=request,
            )  # atomic: any failure (e.g. duplicate owner email) leaves nothing half-created
        except IntegrityError:
            raise ValidationError({"detail": DUPLICATE})
        tenant = _get_tenant(tenant.pk)
        body = summarize(tenant, with_stations=True)
        # Shown ONCE so the technician can hand it over. It is never stored or logged in plain text.
        body["owner_credentials"] = {"login": owner.email or owner.phone, "temporary_password": temporary_password}
        response = Response(body, status=status.HTTP_201_CREATED)
        response["Cache-Control"] = "no-store"
        return response


class PlatformTenantDetailView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request, pk):
        return Response(summarize(_get_tenant(pk), with_stations=True))

    def patch(self, request, pk):
        tenant = _get_tenant(pk)
        data = TenantUpdateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        before = {"name": tenant.name, "status": tenant.status}
        for key, value in data.validated_data.items():
            setattr(tenant, key, value)
        tenant.save()
        after = {"name": tenant.name, "status": tenant.status}
        with tenant_scope(tenant.id):
            audit.log("tenant_updated", actor=request.user, tenant_id=tenant.id, obj=tenant, changes=audit.diff(before, after), request=request)
        return Response(summarize(tenant, with_stations=True))


class PlatformStationView(APIView):
    permission_classes = [IsPlatformAdmin]

    def get(self, request, pk):
        tenant = _get_tenant(pk)
        with tenant_scope(tenant.id):
            return Response(StationSerializer(Station.objects.all(), many=True).data)

    def post(self, request, pk):
        tenant = _get_tenant(pk)
        data = StationInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            with transaction.atomic(), tenant_scope(tenant.id):
                station = Station.objects.create(tenant_id=tenant.id, **data.validated_data)
                audit.log("station_created", actor=request.user, tenant_id=tenant.id, obj=station, changes={"name": station.name}, request=request)
        except IntegrityError:
            raise ValidationError({"name": "A station with this name already exists."})
        return Response(StationSerializer(station).data, status=status.HTTP_201_CREATED)


class PlatformSubscriptionView(APIView):
    permission_classes = [IsPlatformAdmin]

    def _sub(self, pk):
        tenant = _get_tenant(pk)
        sub = getattr(tenant, "subscription", None)
        if sub is None:
            raise NotFound("Subscription not found.")
        return tenant, sub

    def get(self, request, pk):
        return Response(SubscriptionSerializer(self._sub(pk)[1]).data)

    def patch(self, request, pk):
        tenant, sub = self._sub(pk)
        data = SubscriptionUpdateSerializer(sub, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        before = SubscriptionSerializer(sub).data
        sub = data.save()
        after = SubscriptionSerializer(sub).data
        with tenant_scope(tenant.id):
            audit.log("subscription_updated", actor=request.user, tenant_id=tenant.id, obj=sub, changes=audit.diff(dict(before), dict(after)), request=request)
        return Response(after)


class ResetOwnerPasswordView(APIView):
    permission_classes = [IsPlatformAdmin]

    def post(self, request, pk):
        tenant = _get_tenant(pk)
        temporary_password = generate_temporary_password()
        with tenant_scope(tenant.id):
            owners = User.objects.filter(role=Role.OWNER).order_by("created_at")
            if request.data.get("owner_id"):
                owners = owners.filter(pk=request.data["owner_id"])
            owner = owners.first()
            if owner is None:
                raise NotFound("Owner not found.")
            owner.set_password(temporary_password)
            owner.must_change_password = True
            owner.revoke_tokens()
            owner.save(update_fields=["password", "must_change_password", "token_version", "updated_at"])
            audit.log("owner_password_reset", actor=request.user, tenant_id=tenant.id, obj=owner, request=request)
        response = Response({"owner_credentials": {"login": owner.email or owner.phone, "temporary_password": temporary_password}})
        response["Cache-Control"] = "no-store"
        return response


class MySubscriptionView(APIView):
    """A tenant's own subscription (read-only; RLS makes it impossible to see anyone else's).

    Stays reachable while the tenant is locked out, so the 'pay to reactivate' screen can use it.
    """

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        sub = Subscription.objects.first()
        if sub is None:
            raise NotFound("No subscription.")
        return Response({**SubscriptionSerializer(sub).data, "tenant_status": request.user.tenant.status})


class TenantSettingsView(APIView):
    """Owner edits thresholds, branding and daily-summary preferences. Managers can read."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        from .serializers import TenantSettingsSerializer
        from .services import get_settings

        return Response(TenantSettingsSerializer(get_settings(request.user.tenant_id)).data)

    def patch(self, request):
        from .serializers import TenantSettingsSerializer
        from .services import get_settings

        require(request.user, "manage_settings", "Only the owner can change settings.")
        obj = get_settings(request.user.tenant_id)
        before = TenantSettingsSerializer(obj).data
        data = TenantSettingsSerializer(obj, data=request.data, partial=True)
        data.is_valid(raise_exception=True)
        obj = data.save()
        audit.log("settings_updated", actor=request.user, obj=obj, changes=audit.diff({k: str(v) for k, v in before.items()}, {k: str(v) for k, v in TenantSettingsSerializer(obj).data.items()}), request=request)
        return Response(TenantSettingsSerializer(obj).data)


class TenantLogoView(APIView):
    from rest_framework.parsers import FormParser, MultiPartParser

    permission_classes = [IsOwnerOrManager]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        from apps.stations.views import validate_image

        from .serializers import TenantSettingsSerializer
        from .services import get_settings

        require(request.user, "manage_settings")
        if "logo" not in request.FILES:
            raise ValidationError({"detail": "Send a 'logo' image."})
        obj = get_settings(request.user.tenant_id)
        obj.logo = validate_image(request.FILES["logo"], max_mb=2)
        obj.save()
        audit.log("logo_changed", actor=request.user, obj=obj, request=request)
        return Response(TenantSettingsSerializer(obj).data)
