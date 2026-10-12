from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOwnerOrManager, IsTenantUser
from apps.audit import services as audit

from . import services
from .access import scope, visible_station_ids
from .models import CalibrationPoint, ExchangeRate, FuelPrice, FuelType, Nozzle, Pump, Station, StationAssignment, Tank
from .serializers import (
    CalibrationInputSerializer,
    ExchangeRateSerializer,
    FuelPriceSerializer,
    FuelTypeSerializer,
    NozzleSerializer,
    PriceInputSerializer,
    PumpSerializer,
    StationSerializer,
    TankSerializer,
)

DUPLICATE = {"detail": "That already exists, or one of the values is not allowed."}


class SetupPermission(BasePermission):
    """Everyone on staff can look; owners and managers can change."""

    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated and u.role in ("owner", "manager", "attendant")):
            return False
        return request.method in SAFE_METHODS or u.role in ("owner", "manager")


class SetupViewSet(viewsets.ModelViewSet):
    """Tenant setup CRUD. Tenant scoping is Row-Level Security; every change is audited; delete is soft."""

    permission_classes = [SetupPermission]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    station_field = None  # set to scope by the user's assigned stations

    def get_queryset(self):
        qs = self.queryset.all()
        if self.station_field:
            qs = scope(qs, self.request.user, self.station_field)
        station = self.request.query_params.get("station")
        if station and self.station_field:
            qs = qs.filter(**{self.station_field: station})
        return qs

    def _snap(self, obj):
        return {k: str(v) for k, v in self.get_serializer(obj).data.items() if k not in ("nozzles", "qr_token")}

    def perform_create(self, serializer):
        try:
            with transaction.atomic():
                obj = serializer.save(tenant_id=self.request.user.tenant_id)
        except IntegrityError:
            raise ValidationError(DUPLICATE)
        audit.log(f"{obj._meta.model_name}_created", actor=self.request.user, obj=obj, changes=self._snap(obj), request=self.request)

    def perform_update(self, serializer):
        before = self._snap(serializer.instance)
        try:
            with transaction.atomic():
                obj = serializer.save()
        except IntegrityError:
            raise ValidationError(DUPLICATE)
        audit.log(f"{obj._meta.model_name}_updated", actor=self.request.user, obj=obj, changes=audit.diff(before, self._snap(obj)), request=self.request)

    def perform_destroy(self, instance):
        instance.delete()  # soft delete
        audit.log(f"{instance._meta.model_name}_deleted", actor=self.request.user, obj=instance, request=self.request)


class StationViewSet(SetupViewSet):
    queryset = Station.objects.all()
    serializer_class = StationSerializer

    def get_queryset(self):
        return scope(self.queryset.all(), self.request.user, "id")

    def perform_create(self, serializer):
        if self.request.user.role != "owner":
            raise PermissionDenied("Only the owner can add stations.")
        super().perform_create(serializer)

    def perform_destroy(self, instance):
        if self.request.user.role != "owner":
            raise PermissionDenied("Only the owner can remove stations.")
        super().perform_destroy(instance)


class FuelTypeViewSet(SetupViewSet):
    queryset = FuelType.objects.all()
    serializer_class = FuelTypeSerializer


class TankViewSet(SetupViewSet):
    queryset = Tank.objects.select_related("fuel_type").all()
    serializer_class = TankSerializer
    station_field = "station_id"
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]  # PUT is used by /calibration/

    @action(detail=True, methods=["get", "put"], url_path="calibration")
    def calibration(self, request, pk=None):
        tank = self.get_object()
        if request.method == "GET":
            return Response([{"dip_mm": p.dip_mm, "litres": p.litres} for p in tank.calibration.all()])
        if request.user.role not in ("owner", "manager"):
            raise PermissionDenied()
        data = CalibrationInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        with transaction.atomic():
            tank.calibration.all().update(deleted_at=timezone.now())  # old chart kept (soft), new chart replaces it
            for p in data.validated_data["points"]:
                CalibrationPoint.objects.create(tenant_id=tank.tenant_id, tank=tank, dip_mm=p["dip_mm"], litres=p["litres"])
        audit.log("tank_calibration_set", actor=request.user, obj=tank, changes={"points": len(data.validated_data["points"])}, request=request)
        return Response({"points": len(data.validated_data["points"])})


class PumpViewSet(SetupViewSet):
    queryset = Pump.objects.prefetch_related("nozzles").all()
    serializer_class = PumpSerializer
    station_field = "station_id"

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser], url_path="photos")
    def photos(self, request, pk=None):
        """Optional nameplate / display photos, for later hardware research."""
        if request.user.role not in ("owner", "manager"):
            raise PermissionDenied()
        pump = self.get_object()
        changed = []
        for field in ("nameplate_photo", "display_photo"):
            if field in request.FILES:
                setattr(pump, field, validate_image(request.FILES[field]))
                changed.append(field)
        if not changed:
            raise ValidationError({"detail": "Send nameplate_photo and/or display_photo."})
        pump.save()
        audit.log("pump_photos_added", actor=request.user, obj=pump, changes={"fields": changed}, request=request)
        return Response(PumpSerializer(pump).data)


def validate_image(f, max_mb=5):
    if not (f.content_type or "").startswith("image/"):
        raise ValidationError({"detail": "Upload an image file."})
    if f.size > max_mb * 1024 * 1024:
        raise ValidationError({"detail": f"Image is too big (max {max_mb} MB)."})
    return f


class NozzleViewSet(SetupViewSet):
    queryset = Nozzle.objects.select_related("pump").all()
    serializer_class = NozzleSerializer

    def get_queryset(self):
        return scope(self.queryset.all(), self.request.user, "pump__station_id")


class FuelPriceView(APIView):
    """GET: price history (filter station, fuel_type). POST: set a new price (owner/manager). Old rows are never edited."""

    permission_classes = [SetupPermission]

    def get(self, request):
        qs = scope(FuelPrice.objects.select_related("fuel_type").all(), request.user)
        for key in ("station", "fuel_type", "currency"):
            if request.query_params.get(key):
                qs = qs.filter(**{key if key == "currency" else f"{key}_id": request.query_params[key]})
        return Response(FuelPriceSerializer(qs[:200], many=True).data)

    def post(self, request):
        data = PriceInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        station = d["station"]
        if (ids := visible_station_ids(request.user)) is not None and station.pk not in ids:
            raise PermissionDenied()
        price = services.set_price(
            tenant_id=request.user.tenant_id, station=station, fuel_type=d["fuel_type"], currency=d.get("currency") or station.pricing_currency,
            price=d["price_per_litre"], effective_from=d.get("effective_from"), user=request.user,
        )
        audit.log("price_set", actor=request.user, obj=price, changes={"station": station.name, "fuel": d["fuel_type"].code, "price": str(price.price_per_litre), "currency": price.currency, "from": price.effective_from.isoformat()}, request=request)
        return Response(FuelPriceSerializer(price).data, status=status.HTTP_201_CREATED)


class ExchangeRateViewSet(SetupViewSet):
    queryset = ExchangeRate.objects.all()
    serializer_class = ExchangeRateSerializer
    http_method_names = ["get", "post", "head", "options"]  # history is never edited

    def perform_create(self, serializer):
        try:
            with transaction.atomic():
                obj = serializer.save(tenant_id=self.request.user.tenant_id, created_by=self.request.user, effective_from=serializer.validated_data.get("effective_from") or timezone.now())
        except IntegrityError:
            raise ValidationError(DUPLICATE)
        audit.log("exchangerate_created", actor=self.request.user, obj=obj, changes=self._snap(obj), request=self.request)


class StationAssignmentView(APIView):
    """Owner sets which stations a manager/attendant works at. Empty list = all stations."""

    permission_classes = [IsOwnerOrManager]

    def _user(self, pk):
        from apps.accounts.models import User

        user = User.objects.filter(pk=pk).first()
        if not user or user.role not in ("manager", "attendant"):
            raise NotFound()
        return user

    def get(self, request, pk):
        return Response({"station_ids": [str(i) for i in StationAssignment.objects.filter(user_id=self._user(pk).pk).values_list("station_id", flat=True)]})

    def put(self, request, pk):
        if request.user.role != "owner":
            raise PermissionDenied()
        user = self._user(pk)
        ids = request.data.get("station_ids", [])
        stations = list(Station.objects.filter(pk__in=ids))
        if len(stations) != len(set(map(str, ids))):
            raise ValidationError({"station_ids": "Unknown station."})
        with transaction.atomic():
            StationAssignment.objects.filter(user=user).update(deleted_at=timezone.now())
            for s in stations:
                StationAssignment.objects.create(tenant_id=user.tenant_id, user=user, station=s)
        audit.log("stations_assigned", actor=request.user, obj=user, changes={"stations": [s.name for s in stations]}, request=request)
        return Response({"station_ids": [str(s.pk) for s in stations]})


class StationListView(ListAPIView):
    """Kept for the Phase 2 URL."""

    permission_classes = [IsTenantUser]
    serializer_class = StationSerializer

    def get_queryset(self):
        return scope(Station.objects.all(), self.request.user, "id")


def qr_svg_response(url):
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10)
    from io import BytesIO

    buf = BytesIO()
    img.save(buf)
    return HttpResponse(buf.getvalue(), content_type="image/svg+xml")
