import uuid
from datetime import timedelta

from django.apps import apps as django_apps
from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOwnerOrManager, IsTenantUser
from apps.audit import services as audit
from apps.stations.access import scope, visible_station_ids
from apps.stations.models import FuelPrice, Pump, Station, Tank
from apps.stations.views import qr_svg_response, validate_image

from . import services
from .models import Delivery, PumpMeterReading, Shift, SyncOperation, TankReading
from .serializers import (
    CashSerializer,
    CheckpointInput,
    DeliveryInput,
    DeliverySerializer,
    ReadingSerializer,
    ShiftEndInput,
    ShiftSerializer,
    ShiftStartInput,
    TankDipInput,
    TankReadingSerializer,
    TransactionSerializer,
)


def _check_station(user, station):
    ids = visible_station_ids(user)
    if ids is not None and station.pk not in ids:
        raise PermissionDenied("You are not assigned to this station.")


def _own_or_manager(user, shift):
    if shift.attendant_id != user.pk and user.role not in ("owner", "manager"):
        raise PermissionDenied("This is not your shift.")


def shift_detail(shift):
    from apps.reports.reconciliation import cash_summary

    return {
        **ShiftSerializer(shift).data,
        "readings": ReadingSerializer(shift.readings.all(), many=True).data,
        "transactions": TransactionSerializer(shift.transactions.select_related("fuel_type"), many=True).data,
        "cash": CashSerializer(shift.cash.all(), many=True).data,
        "summary": cash_summary(shift),
    }


# ---- core operations (shared by REST and offline sync) ---------------------------------------------------------

def do_shift_start(user, data):
    _check_station(user, data["station"])
    for item in data["readings"]:
        if item["nozzle"].pump.station_id != data["station"].pk:
            raise ValidationError({"readings": "A pump does not belong to this station."})
    try:
        return services.start_shift(user=user, station=data["station"], readings=data["readings"], shift_id=data.get("shift_id"), started_at=data.get("started_at"))
    except services.Conflict as exc:
        raise ValidationError({"detail": str(exc)})


def do_shift_end(user, shift, data, strict=False):
    _own_or_manager(user, shift)
    try:
        return services.end_shift(user=user, shift=shift, readings=data["readings"], cash=data["cash"], ended_at=data.get("ended_at"), strict=strict)
    except services.Conflict as exc:
        raise ValidationError({"detail": str(exc)})


# ---- REST ------------------------------------------------------------------------------------------------------

class PrefillView(APIView):
    """Everything the phone needs to work offline: stations, pumps/nozzles with their last meter number, tanks, prices."""

    permission_classes = [IsTenantUser]

    def get(self, request):
        stations = scope(Station.objects.filter(status="active"), request.user, "id")
        out = []
        horizon = timezone.now() - timedelta(days=2)
        for st in stations:
            pumps = []
            for p in Pump.objects.filter(station=st).select_related("tank__fuel_type").prefetch_related("nozzles"):
                nozzles = []
                for n in p.nozzles.all():
                    last = services.last_end_reading(n)
                    nozzles.append({"id": str(n.pk), "number": n.number, "label": str(n), "meter_max": str(n.meter_max), "last_reading": str(last.reading) if last else None})
                pumps.append({"id": str(p.pk), "name": p.name, "qr_token": p.qr_token, "data_source": p.data_source, "fuel_type": str(p.tank.fuel_type_id), "fuel_code": p.tank.fuel_type.code, "nozzles": nozzles})
            tanks = []
            for t in Tank.objects.filter(station=st).select_related("fuel_type"):
                last = TankReading.objects.filter(tank=t).first()
                tanks.append({"id": str(t.pk), "name": t.name, "qr_token": t.qr_token, "fuel_code": t.fuel_type.code, "capacity_litres": str(t.capacity_litres), "has_calibration": t.calibration.count() >= 2, "last_litres": str(last.litres) if last else None})
            prices = FuelPrice.objects.filter(station=st).exclude(effective_to__lt=horizon).order_by("effective_from")
            open_shift = Shift.objects.filter(station=st, attendant=request.user, status="open").first()
            out.append({
                "id": str(st.pk), "name": st.name, "pricing_currency": st.pricing_currency, "pumps": pumps, "tanks": tanks,
                "prices": [{"fuel_type": str(x.fuel_type_id), "currency": x.currency, "price": str(x.price_per_litre), "from": x.effective_from, "to": x.effective_to} for x in prices],
                "open_shift_id": str(open_shift.pk) if open_shift else None,
            })
        return Response({"stations": out, "server_time": timezone.now()})


class ShiftListView(ListAPIView):
    permission_classes = [IsTenantUser]
    serializer_class = ShiftSerializer

    def get_queryset(self):
        qs = scope(Shift.objects.select_related("attendant", "station"), self.request.user)
        if self.request.user.role == "attendant":
            qs = qs.filter(attendant=self.request.user)
        q = self.request.query_params
        for key, lookup in (("station", "station_id"), ("status", "status"), ("attendant", "attendant_id")):
            if q.get(key):
                qs = qs.filter(**{lookup: q[key]})
        if q.get("date_from"):
            qs = qs.filter(started_at__date__gte=q["date_from"])
        if q.get("date_to"):
            qs = qs.filter(started_at__date__lte=q["date_to"])
        return qs


class ShiftDetailView(APIView):
    permission_classes = [IsTenantUser]

    def get(self, request, pk):
        shift = _get_shift(request, pk)
        return Response(shift_detail(shift))


def _get_shift(request, pk):
    try:
        shift = scope(Shift.objects.select_related("attendant", "station"), request.user).filter(pk=pk).first()
    except Exception:
        shift = None
    if shift is None or (request.user.role == "attendant" and shift.attendant_id != request.user.pk):
        raise NotFound("Shift not found.")
    return shift


class ShiftStartView(APIView):
    permission_classes = [IsTenantUser]

    def post(self, request):
        data = ShiftStartInput(data=request.data)
        data.is_valid(raise_exception=True)
        shift = do_shift_start(request.user, data.validated_data)
        audit.log("shift_started", actor=request.user, obj=shift, request=request)
        return Response(shift_detail(shift), status=status.HTTP_201_CREATED)


class ShiftEndView(APIView):
    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        data = ShiftEndInput(data=request.data)
        data.is_valid(raise_exception=True)
        shift = do_shift_end(request.user, shift, data.validated_data, strict=True)
        audit.log("shift_ended", actor=request.user, obj=shift, changes={"flags": shift.flags}, request=request)
        return Response(shift_detail(shift))


class ShiftCheckpointView(APIView):
    """Mid-shift meter number (used when the price changes during a shift)."""

    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        _own_or_manager(request.user, shift)
        data = CheckpointInput(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            services.add_checkpoint(user=request.user, shift=shift, nozzle=data.validated_data["nozzle"], reading=data.validated_data["reading"], at=data.validated_data.get("at"))
        except services.Conflict as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(shift_detail(shift), status=status.HTTP_201_CREATED)


class ShiftReviewView(APIView):
    permission_classes = [IsOwnerOrManager]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        services.review_shift(user=request.user, shift=shift, notes=request.data.get("notes", ""))
        audit.log("shift_reviewed", actor=request.user, obj=shift, request=request)
        return Response(shift_detail(shift))


class ReadingPhotoView(APIView):
    """Optional evidence photo for a meter or dip reading, uploaded after the number has synced."""

    permission_classes = [IsTenantUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, kind, pk):
        model = {"meter": PumpMeterReading, "dip": TankReading}.get(kind)
        if model is None or "photo" not in request.FILES:
            raise ValidationError({"detail": "Send a photo."})
        row = model.objects.filter(pk=pk).first()
        if row is None:
            raise NotFound()
        row.photo = validate_image(request.FILES["photo"])
        row.save(update_fields=["photo", "updated_at"])
        return Response({"ok": True})


class TankDipView(APIView):
    permission_classes = [IsTenantUser]

    def get(self, request):
        qs = scope(TankReading.objects.all(), request.user, "tank__station_id")
        if request.query_params.get("tank"):
            qs = qs.filter(tank_id=request.query_params["tank"])
        return Response(TankReadingSerializer(qs[:200], many=True).data)

    def post(self, request):
        data = TankDipInput(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        _check_station(request.user, d["tank"].station)
        row = services.record_tank_dip(user=request.user, tank=d["tank"], litres=d.get("litres"), dip_mm=d.get("dip_mm"), at=d.get("at"), shift=d.get("shift"))
        return Response(TankReadingSerializer(row).data, status=status.HTTP_201_CREATED)


class DeliveryView(APIView):
    permission_classes = [IsTenantUser]

    def get(self, request):
        qs = scope(Delivery.objects.all(), request.user, "tank__station_id")
        if request.query_params.get("tank"):
            qs = qs.filter(tank_id=request.query_params["tank"])
        return Response(DeliverySerializer(qs[:200], many=True).data)

    def post(self, request):
        data = DeliveryInput(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        _check_station(request.user, d["tank"].station)
        row = services.record_delivery(user=request.user, tank=d["tank"], litres=d["litres"], delivered_at=d.get("delivered_at"), supplier=d["supplier"], note_number=d["note_number"])
        audit.log("delivery_recorded", actor=request.user, obj=row, changes={"litres": str(row.litres_received), "tank": d["tank"].name}, request=request)
        return Response(DeliverySerializer(row).data, status=status.HTTP_201_CREATED)


# ---- QR codes --------------------------------------------------------------------------------------------------

class QRResolveView(APIView):
    """One scan -> the right entry screen. Works for pump and tank codes."""

    permission_classes = [IsTenantUser]

    def get(self, request, token):
        pump = scope(Pump.objects.select_related("station"), request.user).filter(qr_token=token).first()
        if pump:
            return Response({"type": "pump", "id": str(pump.pk), "station_id": str(pump.station_id), "name": pump.name})
        tank = scope(Tank.objects.select_related("station"), request.user).filter(qr_token=token).first()
        if tank:
            return Response({"type": "tank", "id": str(tank.pk), "station_id": str(tank.station_id), "name": tank.name})
        raise NotFound("Unknown code.")


class QRImageView(APIView):
    permission_classes = [IsOwnerOrManager]

    def get(self, request, kind, pk):
        model = {"pumps": Pump, "tanks": Tank}.get(kind)
        obj = model.objects.filter(pk=pk).first() if model else None
        if obj is None:
            raise NotFound()
        return qr_svg_response(f"{settings.FRONTEND_BASE_URL}/q/{obj.qr_token}")


# ---- private media (photos) ------------------------------------------------------------------------------------

MEDIA_FIELDS = {
    ("stations", "pump"): {"nameplate_photo", "display_photo"},
    ("operations", "pumpmeterreading"): {"photo"},
    ("operations", "tankreading"): {"photo"},
    ("tenants", "tenantsettings"): {"logo"},
}


class MediaView(APIView):
    """Photos are private to their tenant: the row is looked up through RLS before the file is served."""

    permission_classes = [IsTenantUser]

    def get(self, request, model, pk, field):
        for (app_label, name), fields in MEDIA_FIELDS.items():
            if name == model and field in fields:
                obj = django_apps.get_model(app_label, name).objects.filter(pk=pk).first()
                break
        else:
            raise Http404
        f = getattr(obj, field, None) if obj else None
        if not f:
            raise Http404
        if getattr(settings, "USE_S3_MEDIA", False):
            return HttpResponseRedirect(f.url)  # short-lived presigned URL
        return FileResponse(f.open("rb"))


# ---- offline sync ----------------------------------------------------------------------------------------------

def _apply(user, kind, p):
    """One queued entry from a phone. Never blocks on odd numbers (they are flagged); only truly impossible
    entries (unknown ids, reviewed shifts) are rejected and kept for a manager."""
    if kind == "shift_start":
        data = ShiftStartInput(data=p)
        data.is_valid(raise_exception=True)
        shift = do_shift_start(user, data.validated_data)
        return {"shift_id": str(shift.pk)}
    if kind in ("shift_end", "checkpoint"):
        shift = Shift.objects.filter(pk=p.get("shift_id")).first()
        if shift is None:
            raise ValidationError({"detail": "Unknown shift."})
        if kind == "checkpoint":
            data = CheckpointInput(data=p)
            data.is_valid(raise_exception=True)
            _own_or_manager(user, shift)
            try:
                services.add_checkpoint(user=user, shift=shift, nozzle=data.validated_data["nozzle"], reading=data.validated_data["reading"], at=data.validated_data.get("at"))
            except services.Conflict as exc:
                raise ValidationError({"detail": str(exc)})
            return {"shift_id": str(shift.pk)}
        data = ShiftEndInput(data=p)
        data.is_valid(raise_exception=True)
        shift = do_shift_end(user, shift, data.validated_data)
        return {"shift_id": str(shift.pk), "flags": shift.flags}
    if kind == "tank_dip":
        data = TankDipInput(data=p)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        row = services.record_tank_dip(user=user, tank=d["tank"], litres=d.get("litres"), dip_mm=d.get("dip_mm"), at=d.get("at"), shift=d.get("shift"))
        return {"id": str(row.pk)}
    if kind == "delivery":
        data = DeliveryInput(data=p)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        row = services.record_delivery(user=user, tank=d["tank"], litres=d["litres"], delivered_at=d.get("delivered_at"), supplier=d["supplier"], note_number=d["note_number"])
        return {"id": str(row.pk)}
    raise ValidationError({"detail": f"Unknown entry type '{kind}'."})


def _error_text(exc):
    detail = getattr(exc, "detail", None)
    return str(detail if detail is not None else exc)[:500]


class SyncView(APIView):
    """Offline sync. The phone sends its queue; each entry carries a client-made op_id, so retrying is always safe.

    Rules: entries apply in the order sent; a repeated op_id returns the stored result and changes nothing;
    meter numbers that look odd are saved and flagged, never refused; a correction to an unreviewed shift
    replaces the old number (the audit log keeps it); entries for a reviewed shift are rejected and listed
    for a manager."""

    permission_classes = [IsTenantUser]

    def post(self, request):
        ops = request.data.get("ops")
        if not isinstance(ops, list) or len(ops) > 200:
            raise ValidationError({"detail": "Send up to 200 entries in 'ops'."})
        results = []
        for op in ops:
            try:
                op_id = uuid.UUID(str(op.get("op_id")))
            except (ValueError, AttributeError):
                results.append({"op_id": None, "status": "rejected", "error": "Missing or invalid op_id."})
                continue
            prior = SyncOperation.objects.filter(op_id=op_id).first()
            if prior:
                results.append({"op_id": str(op_id), "status": prior.status, "duplicate": True, **({"result": prior.result} if prior.status == "applied" else {"error": prior.result.get("error")})})
                continue
            kind = str(op.get("kind", ""))
            try:
                with transaction.atomic():
                    result = _apply(request.user, kind, op.get("payload") or {})
                    SyncOperation.objects.create(tenant_id=request.user.tenant_id, op_id=op_id, kind=kind, status="applied", result=result, user=request.user)
                results.append({"op_id": str(op_id), "status": "applied", "result": result})
            except Exception as exc:  # noqa: BLE001 - a bad entry must never stop the rest of the queue
                if not isinstance(exc, (ValidationError, PermissionDenied, services.Conflict, NotFound)):
                    raise
                text = _error_text(exc)
                SyncOperation.objects.create(tenant_id=request.user.tenant_id, op_id=op_id, kind=kind, status="rejected", result={"error": text, "payload": op.get("payload")}, user=request.user)
                results.append({"op_id": str(op_id), "status": "rejected", "error": text})
        return Response({"results": results})


class SyncRejectedView(APIView):
    """Entries the server could not apply, for a manager to look at."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        rows = SyncOperation.objects.filter(status="rejected").select_related("user").order_by("-created_at")[:100]
        return Response([{"op_id": str(r.op_id), "kind": r.kind, "user": r.user.full_name, "error": r.result.get("error"), "at": r.created_at} for r in rows])
