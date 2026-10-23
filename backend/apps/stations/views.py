from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import Allow, IsOwnerOrManager, IsTenantUser, SetupPermission, can, require
from apps.audit import services as audit

from . import services
from .access import scope, visible_station_ids
from .models import CalibrationPoint, ExchangeRate, FuelPrice, FuelType, Nozzle, Pump, Station, StationAssignment, Tank, pump_nozzle_problem
from .serializers import (
    CalibrationInputSerializer,
    ExchangeRateSerializer,
    FuelPriceOwnerSerializer,
    FuelPriceSerializer,
    FuelTypeSerializer,
    NozzleSerializer,
    PriceInputSerializer,
    PumpSerializer,
    StationSerializer,
    TankSerializer,
)

DUPLICATE = {"detail": "That already exists, or one of the values is not allowed."}
ANNOUNCE_HOURS = 48  # a new price stays on everyone's home screen this long after it starts (and before, if set ahead)
OWNER_ANNOUNCE_HOURS = 24  # the owner sees a price change while it is new: 24 hours from when it was set or started


def price_serializer(user):
    """Only the owner is told who set a price (PLAN.md rule 11: the server decides what each role receives)."""
    return FuelPriceOwnerSerializer if can(user, "see_price_setter") else FuelPriceSerializer


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
        return {k: str(v) for k, v in self.get_serializer(obj).data.items() if k != "nozzles"}

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
        require(self.request.user, "manage_stations", "Only the owner can add stations.")
        from apps.tenants.features import SYSTEM_POLICY
        from apps.tenants.models import Tenant

        # A new station follows the pump system the platform chose for this business.
        tenant = Tenant.objects.get(pk=self.request.user.tenant_id)
        mode, assignment = SYSTEM_POLICY.get(tenant.pump_system, SYSTEM_POLICY["shared"])
        serializer.validated_data.update(attendants_mode=mode, assignment_mode=assignment)
        super().perform_create(serializer)

    def perform_destroy(self, instance):
        require(self.request.user, "manage_stations", "Only the owner can remove stations.")
        super().perform_destroy(instance)


class FuelTypeViewSet(SetupViewSet):
    queryset = FuelType.objects.all()
    serializer_class = FuelTypeSerializer


class TankViewSet(SetupViewSet):
    """Tanks hold tank levels and calibration, so attendants are refused even read access (rule 11)."""

    permission_classes = [Allow("edit_setup", read="view_tank_levels")]
    queryset = Tank.objects.select_related("fuel_type").all()
    serializer_class = TankSerializer
    station_field = "station_id"
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]  # PUT is used by /calibration/

    @action(detail=True, methods=["get", "put"], url_path="calibration")
    def calibration(self, request, pk=None):
        tank = self.get_object()
        if request.method == "GET":
            return Response([{"dip_mm": p.dip_mm, "litres": p.litres} for p in tank.calibration.all()])
        require(request.user, "edit_setup")
        data = CalibrationInputSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        with transaction.atomic():
            tank.calibration.all().update(deleted_at=timezone.now())  # old chart kept (soft), new chart replaces it
            for p in data.validated_data["points"]:
                CalibrationPoint.objects.create(tenant_id=tank.tenant_id, tank=tank, dip_mm=p["dip_mm"], litres=p["litres"])
        audit.log("tank_calibration_set", actor=request.user, obj=tank, changes={"points": len(data.validated_data["points"])}, request=request)
        return Response({"points": len(data.validated_data["points"])})


class PumpViewSet(SetupViewSet):
    queryset = Pump.objects.select_related("assigned_to").prefetch_related("nozzles__tank__fuel_type").all()
    serializer_class = PumpSerializer
    station_field = "station_id"

    def perform_create(self, serializer):
        station = serializer.validated_data["station"]
        if (ids := visible_station_ids(self.request.user)) is not None and station.pk not in ids:
            raise PermissionDenied("You are not assigned to this station.")
        super().perform_create(serializer)

    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, pk=None):
        """Assigned-pump stations: a manager or owner gives this pump to an attendant (or takes it back with user=null).
        If the pump is in use on someone's open shift, this is a swap: send the meter number of both nozzles now."""
        from apps.operations.serializers import PumpAssignInput
        from apps.operations.services import Conflict, assign_pump

        require(request.user, "manage_shift", "Only a manager or the owner assigns pumps.")
        pump = self.get_object()
        data = PumpAssignInput(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        try:
            assign_pump(user=request.user, pump=pump, to_user=d.get("user"), readings=d.get("readings") or [], at=d.get("at"), strict=True, request=request)
        except Conflict as exc:
            raise ValidationError({"detail": str(exc)})
        pump.refresh_from_db()
        return Response(PumpSerializer(pump).data)

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser], url_path="photos")
    def photos(self, request, pk=None):
        """Optional nameplate / display photos, for later hardware research."""
        require(request.user, "edit_setup")
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
    """A pump's nozzles: more can be added, and one can be removed, as long as the pump keeps at least one."""

    queryset = Nozzle.objects.select_related("pump", "tank__fuel_type").all()
    serializer_class = NozzleSerializer

    def perform_create(self, serializer):
        station_id = serializer.validated_data["pump"].station_id
        if (ids := visible_station_ids(self.request.user)) is not None and station_id not in ids:
            raise PermissionDenied("You are not assigned to this station.")
        super().perform_create(serializer)

    def perform_destroy(self, instance):
        rest = [n.tank_id for n in instance.pump.nozzles.exclude(pk=instance.pk)]
        if problem := pump_nozzle_problem(rest):
            raise ValidationError({"detail": f"{problem} Add another nozzle before removing this one."})
        super().perform_destroy(instance)

    def get_queryset(self):
        return scope(self.queryset.all(), self.request.user, "pump__station_id")


class FuelPriceView(APIView):
    """GET: price history (filter station, fuel_type). POST: set a new price (owner/manager). Old rows are never edited."""

    permission_classes = [SetupPermission]

    def get(self, request):
        qs = scope(FuelPrice.objects.select_related("fuel_type", "created_by").all(), request.user)
        for key in ("station", "fuel_type", "currency"):
            if request.query_params.get(key):
                qs = qs.filter(**{key if key == "currency" else f"{key}_id": request.query_params[key]})
        return Response(price_serializer(request.user)(qs[:200], many=True).data)

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
        return Response(price_serializer(request.user)(price).data, status=status.HTTP_201_CREATED)


class PriceAnnouncementView(APIView):
    """Price changes everyone at the station is told about: prices that started in the last ANNOUNCE_HOURS and are
    still in force, and prices set to start later. The owner sees each one for OWNER_ANNOUNCE_HOURS from when it was
    set or started (whichever is later), with who set it and when."""

    permission_classes = [Allow("view_setup")]

    def get(self, request):
        now = timezone.now()
        rows = list(
            scope(FuelPrice.objects.select_related("station", "fuel_type", "created_by"), request.user)
            .filter(station__status=Station.Status.ACTIVE, effective_from__gte=now - timedelta(hours=ANNOUNCE_HOURS))
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=now))
            .order_by("-effective_from")[:20]
        )
        # The price each one replaced: the row in the same series that ended the instant it started.
        previous = {
            (p.station_id, p.fuel_type_id, p.currency, p.effective_to): p.price_per_litre
            for p in FuelPrice.objects.filter(station_id__in={r.station_id for r in rows}, effective_to__in={r.effective_from for r in rows})
        }
        owner = can(request.user, "see_price_setter")
        if owner:  # the owner only while it is new: 24 hours from when it was set or started, whichever is later
            since = now - timedelta(hours=OWNER_ANNOUNCE_HOURS)
            rows = [r for r in rows if max(r.effective_from, r.created_at) >= since]
        out = []
        for r in rows:
            item = {
                "id": str(r.pk), "station": str(r.station_id), "station_name": r.station.name, "fuel": r.fuel_type.name,
                "currency": r.currency, "price_per_litre": str(r.price_per_litre), "effective_from": r.effective_from,
                "previous_price": (str(prev) if (prev := previous.get((r.station_id, r.fuel_type_id, r.currency, r.effective_from))) else None),
                "upcoming": r.effective_from > now,
            }
            if owner:
                item["set_by"] = r.created_by.full_name if r.created_by else None
                item["set_by_role"] = r.created_by.role if r.created_by else None
                item["set_at"] = r.created_at
            out.append(item)
        return Response({"announcements": out})


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
    """Owner sets which stations a manager/attendant works at. Empty list = all stations.

    One manager per station: a manager runs one station, and a station has at most one manager."""

    permission_classes = [IsOwnerOrManager]

    def _user(self, pk):
        from apps.accounts.models import User

        user = User.objects.filter(pk=pk).first()
        if not user or user.role not in (Role.MANAGER, Role.ATTENDANT):
            raise NotFound()
        return user

    def get(self, request, pk):
        return Response({"station_ids": [str(i) for i in StationAssignment.objects.filter(user_id=self._user(pk).pk).values_list("station_id", flat=True)]})

    def put(self, request, pk):
        require(request.user, "manage_stations")
        user = self._user(pk)
        ids = request.data.get("station_ids", [])
        stations = list(Station.objects.filter(pk__in=ids))
        if len(stations) != len(set(map(str, ids))):
            raise ValidationError({"station_ids": "Unknown station."})
        if user.role == Role.MANAGER:
            if len(stations) > 1:
                raise ValidationError({"station_ids": "A manager runs one station."})
            for s in stations:
                taken = (StationAssignment.objects.filter(station=s, user__role=Role.MANAGER, user__is_active=True)
                         .exclude(user=user).select_related("user").first())
                if taken:
                    raise ValidationError({"station_ids": f"{taken.user.full_name} already manages {s.name}. A station has one manager."})
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
