import uuid
from datetime import timedelta

from django.apps import apps as django_apps
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Role, User
from apps.accounts.permissions import Allow, IsOwner, IsOwnerOrManager, IsTenantUser, TankRecords, can, require
from apps.audit import services as audit
from apps.stations.access import scope, visible_station_ids
from apps.stations.models import FuelPrice, Pump, Station, StationAssignment, Tank
from apps.stations.views import validate_image

from . import approvals, imports, services
from .models import Delivery, PumpAssignment, PumpMeterReading, Shift, ShiftAttendant, SyncOperation, TankReading, UndoRequest
from .serializers import (
    CashInput,
    CashInputOnly,
    CashSerializer,
    CheckpointInput,
    DeliveryInput,
    DeliverySerializer,
    PersonInput,
    PumpAssignmentSerializer,
    ReadingInput,
    ReadingSerializer,
    ReassignInput,
    ResolveInput,
    ShiftAttendantSerializer,
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


def shift_detail(shift, user=None):
    from apps.reports.reconciliation import cash_summary

    return {
        **ShiftSerializer(shift).data,
        "attendants": ShiftAttendantSerializer(shift.attendants.select_related("user"), many=True).data,
        "assignments": PumpAssignmentSerializer(shift.assignments.select_related("user", "pump"), many=True).data,
        "pending": [a.user.full_name for a in services.pending_people(shift)],
        "readings": ReadingSerializer(shift.readings.select_related("recorded_by"), many=True).data,
        "transactions": TransactionSerializer(shift.transactions.select_related("fuel_type"), many=True).data,
        "cash": CashSerializer(shift.cash.select_related("attendant"), many=True).data,
        "summary": cash_summary(shift),
        **({"you": {"on_shift": services.is_on_shift(user, shift), "can_manage": services.can_manage(user, shift)}} if user else {}),
    }


def _conflict_as_400(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except services.Conflict as exc:
        raise ValidationError({"detail": str(exc)})


def _user(pk, tenant_id):
    if not pk:
        return None
    try:
        found = User.objects.filter(pk=pk).first()
    except DjangoValidationError:
        found = None
    if found is None or found.tenant_id != tenant_id:
        raise ValidationError({"user": "Unknown person."})
    return found


# ---- core operations (shared by REST and offline sync) ---------------------------------------------------------

def do_shift_start(user, data, strict=False):
    _check_station(user, data["station"])
    return _conflict_as_400(
        services.start_shift, user=user, station=data["station"], readings=data["readings"], shift_id=data.get("shift_id"),
        started_at=data.get("started_at"), attendants=data.get("attendants"), assignments=data.get("assignments"), strict=strict,
    )


def do_shift_end(user, shift, data, strict=False, request=None):
    return _conflict_as_400(
        services.end_shift, user=user, shift=shift, readings=data["readings"], cash=data["cash"], ended_at=data.get("ended_at"),
        strict=strict, close=data.get("close"), who=_user(data.get("user"), user.tenant_id), request=request,
    )


def _resolve_shift(user, shift_id):
    """A shift id from a phone. If that phone started the shift offline and it was merged into the station's open shift,
    the id it made up still works: it resolves to the shift it was merged into."""
    try:
        shift = Shift.objects.filter(pk=shift_id).select_related("station").first()
    except DjangoValidationError:
        shift = None
    if shift is None and shift_id:
        alias = SyncOperation.objects.filter(kind="shift_start", status="applied", result__client_shift_id=str(shift_id)).first()
        if alias:
            shift = Shift.objects.filter(pk=alias.result["shift_id"]).select_related("station").first()
    if shift is None:
        raise ValidationError({"detail": "Unknown shift."})
    _check_station(user, shift.station)
    return shift


# ---- REST ------------------------------------------------------------------------------------------------------

def _station_staff(station):
    """Attendants who may work at this station (no station assignment = every station), and whether each is on shift
    here now (a free pump given to them then starts at the meter numbers the manager enters)."""
    limited = set(StationAssignment.objects.values_list("user_id", flat=True))
    here = set(StationAssignment.objects.filter(station=station).values_list("user_id", flat=True))
    on = set(ShiftAttendant.objects.filter(shift__station=station, shift__status=Shift.Status.OPEN, left_at__isnull=True).values_list("user_id", flat=True))
    return [{"id": str(u.pk), "name": u.full_name, "on_shift": u.pk in on}
            for u in User.objects.filter(role=Role.ATTENDANT, is_active=True) if u.pk not in limited or u.pk in here]


class PrefillView(APIView):
    """Everything the phone needs to work offline: stations, pumps/nozzles with their last meter number and fuel,
    prices, the station's attendant policy and the open shift (mine, or one to join with one tap).

    On assigned-pump stations an attendant receives only the pumps a manager gave them (and any still on their open
    shift); managers and owners receive every pump with who works it."""

    permission_classes = [IsTenantUser]

    def get(self, request):
        user = request.user
        stations = scope(Station.objects.filter(status="active"), user, "id")
        out = []
        horizon = timezone.now() - timedelta(days=2)
        manager = can(user, "manage_shift")
        for st in stations:
            assigned = services.is_assigned_station(st)
            qs = Pump.objects.filter(station=st).select_related("assigned_to").prefetch_related("nozzles__tank__fuel_type")
            if assigned and not manager:
                on_my_shift = PumpAssignment.objects.filter(user=user, ended_at__isnull=True, shift__status="open", shift__station=st).values("pump_id")
                qs = qs.filter(Q(assigned_to=user) | Q(pk__in=on_my_shift))
            pumps = []
            for p in qs:
                nozzles = []
                for n in p.nozzles.all():
                    last = services.last_end_reading(n)
                    nozzles.append({"id": str(n.pk), "number": n.number, "label": str(n), "meter_max": str(n.meter_max), "last_reading": str(last.reading) if last else None,
                                    "fuel_type": str(n.tank.fuel_type_id), "fuel_code": n.tank.fuel_type.code})
                pump = {"id": str(p.pk), "name": p.name, "data_source": p.data_source, "nozzles": nozzles}
                if assigned:
                    pump.update(assigned_to=str(p.assigned_to_id) if p.assigned_to_id else None, assigned_to_name=p.assigned_to.full_name if p.assigned_to_id else None)
                    # who is on it right now. Managers: changing a pump in use waits for a handover, or is a swap now.
                    # Attendants: a pump of theirs still with the outgoing attendant passes over when that person ends.
                    busy = services.open_pump_assignment(p)
                    if manager or (busy and busy.user_id != user.pk):
                        pump["in_use_by"] = busy.user.full_name if busy else None
                pumps.append(pump)
            prices = FuelPrice.objects.filter(station=st).exclude(effective_to__lt=horizon).order_by("effective_from")
            open_shifts = list(Shift.objects.filter(station=st, status="open").order_by("started_at"))
            mine = next((s for s in open_shifts if services.is_on_shift(user, s)), None)
            joinable = open_shifts[0] if (open_shifts and mine is None and st.attendants_mode == Station.AttendantsMode.MULTIPLE and not assigned) else None
            entry = {
                "id": str(st.pk), "name": st.name, "pricing_currency": st.pricing_currency, "pumps": pumps,
                "attendants_mode": st.attendants_mode, "assignment_mode": st.assignment_mode,
                "prices": [{"fuel_type": str(x.fuel_type_id), "currency": x.currency, "price": str(x.price_per_litre), "from": x.effective_from, "to": x.effective_to} for x in prices],
                "open_shift_id": str(mine.pk) if mine else None,
                "my_stage": services.phone_stage(user, st),  # the screen this person should be on right now
                # one-attendant station with someone else's shift still being worked: nobody else can start
                "busy_shift": ({"people": [a.user.full_name for a in services.active_attendants(open_shifts[0])]}
                               if (st.attendants_mode == Station.AttendantsMode.SINGLE and open_shifts and mine is None
                                   and services.active_attendants(open_shifts[0]).exists()) else None),
                "joinable_shift": {"id": str(joinable.pk), "started_at": joinable.started_at, "people": [a.user.full_name for a in services.active_attendants(joinable)]} if joinable else None,
            }
            if st.attendants_mode == Station.AttendantsMode.MULTIPLE:
                entry["staff"] = _station_staff(st)
            if can(user, "view_tank_levels"):  # tank levels are for managers and owners only (rule 11)
                tanks = []
                for t in Tank.objects.filter(station=st).select_related("fuel_type"):
                    last = TankReading.objects.filter(tank=t).first()
                    tanks.append({"id": str(t.pk), "name": t.name, "fuel_code": t.fuel_type.code, "capacity_litres": str(t.capacity_litres), "has_calibration": t.calibration.count() >= 2, "last_litres": str(last.litres) if last else None})
                entry["tanks"] = tanks
            out.append(entry)
        return Response({"stations": out, "server_time": timezone.now()})


def _my_shifts(user, qs):
    if can(user, "see_all_shifts"):
        return qs
    return qs.filter(pk__in=ShiftAttendant.objects.filter(user=user).values("shift_id"))


class ShiftListView(ListAPIView):
    permission_classes = [IsTenantUser]
    serializer_class = ShiftSerializer

    def get_queryset(self):
        user = self.request.user
        qs = _my_shifts(user, scope(Shift.objects.select_related("station").prefetch_related("attendants__user"), user))
        q = self.request.query_params
        for key, lookup in (("station", "station_id"), ("status", "status")):
            if q.get(key):
                qs = qs.filter(**{lookup: q[key]})
        if q.get("attendant"):
            qs = qs.filter(pk__in=ShiftAttendant.objects.filter(user_id=q["attendant"]).values("shift_id"))
        if q.get("date_from"):
            qs = qs.filter(started_at__date__gte=q["date_from"])
        if q.get("date_to"):
            qs = qs.filter(started_at__date__lte=q["date_to"])
        return qs


def _get_shift(request, pk, *, joining=False):
    """Managers see the shifts of their stations; an attendant sees shifts they are (or were) on, and, to join,
    an open shift at a station they work at."""
    user = request.user
    try:
        qs = scope(Shift.objects.select_related("station"), user)
        if not joining:
            qs = _my_shifts(user, qs)
        shift = qs.filter(pk=pk).first()
    except DjangoValidationError:
        shift = None
    if shift is None:
        raise NotFound("Shift not found.")
    return shift


class ShiftDetailView(APIView):
    permission_classes = [IsTenantUser]

    def get(self, request, pk):
        return Response(shift_detail(_get_shift(request, pk), request.user))


class ShiftStartView(APIView):
    """Start a shift. An attendant becomes its lead; a manager names the attendants. On assigned pumps an attendant
    starts their own shift on the pumps a manager gave them. On a several-attendants station with shared pumps and a
    shift already open, this joins it."""

    permission_classes = [IsTenantUser]

    def post(self, request):
        data = ShiftStartInput(data=request.data)
        data.is_valid(raise_exception=True)
        shift = do_shift_start(request.user, data.validated_data, strict=True)
        merged = shift.merged_from is not None
        audit.log("shift_joined" if merged else "shift_started", actor=request.user, obj=shift, request=request)
        return Response({**shift_detail(shift, request.user), "merged": merged}, status=status.HTTP_201_CREATED)


class ShiftEndView(APIView):
    """'End of my shift': end meter numbers, my cash, and (lead or manager) close. The shift closes once everyone on
    it has handed in or been marked as left; until then the response lists who is pending."""

    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        data = ShiftEndInput(data=request.data)
        data.is_valid(raise_exception=True)
        shift = do_shift_end(request.user, shift, data.validated_data, strict=True, request=request)
        audit.log("shift_ended", actor=request.user, obj=shift, changes={"flags": shift.flags, "status": shift.status}, request=request)
        return Response(shift_detail(shift, request.user))


class ShiftJoinView(APIView):
    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk, joining=True)
        data = PersonInput(data=request.data)
        data.is_valid(raise_exception=True)
        who = _user(data.validated_data.get("user"), request.user.tenant_id)
        _conflict_as_400(services.join_shift, user=request.user, shift=shift, who=who, at=data.validated_data.get("at"), request=request)
        return Response(shift_detail(shift, request.user))


class ShiftLeaveView(APIView):
    """Leave mid-shift, or (lead/manager) mark someone as left so the shift can close."""

    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        data = PersonInput(data=request.data)
        data.is_valid(raise_exception=True)
        who = _user(data.validated_data.get("user"), request.user.tenant_id)
        _conflict_as_400(services.leave_shift, user=request.user, shift=shift, who=who, at=data.validated_data.get("at"), request=request)
        shift.refresh_from_db()
        return Response(shift_detail(shift, request.user))


class ShiftCashView(APIView):
    """Hand in my cash by payment type (or, lead/manager, record it for someone)."""

    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        data = CashInputOnly(data=request.data)
        data.is_valid(raise_exception=True)
        services.require_on_shift(request.user, shift)
        who = _user(data.validated_data.get("user"), request.user.tenant_id)
        _conflict_as_400(services.submit_cash, user=request.user, shift=shift, cash=data.validated_data["cash"], who=who, request=request)
        shift.refresh_from_db()
        return Response(shift_detail(shift, request.user))


class ShiftCloseView(APIView):
    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        _conflict_as_400(services.request_close, user=request.user, shift=shift, strict=True, request=request)
        shift.refresh_from_db()
        return Response(shift_detail(shift, request.user))


class ShiftReassignView(APIView):
    """Assigned pumps: a manager or owner hands a pump to someone else mid-shift at a meter snapshot (break, swap).
    Same as POST /api/pumps/<id>/assign/; attendants are refused."""

    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        data = ReassignInput(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        _conflict_as_400(services.reassign_pump, user=request.user, shift=shift, pump=d["pump"], to_user=_user(d["user"], request.user.tenant_id),
                         readings=d["readings"], at=d.get("at"), request=request)
        return Response(shift_detail(shift, request.user))


class ShiftResolveReadingView(APIView):
    """Two people entered different numbers for one nozzle: the lead or a manager keeps the right one."""

    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        data = ResolveInput(data=request.data)
        data.is_valid(raise_exception=True)
        _conflict_as_400(services.resolve_readings, user=request.user, shift=shift, keep=data.validated_data["reading"], request=request)
        shift.refresh_from_db()
        return Response(shift_detail(shift, request.user))


class ShiftCheckpointView(APIView):
    """Mid-shift meter number (used when the price changes during a shift)."""

    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        data = CheckpointInput(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        _conflict_as_400(services.add_checkpoint, user=request.user, shift=shift, nozzle=d["nozzle"], reading=d["reading"], at=d.get("at"))
        return Response(shift_detail(shift, request.user), status=status.HTTP_201_CREATED)


class ShiftReviewView(APIView):
    permission_classes = [IsOwnerOrManager]

    def post(self, request, pk):
        shift = _get_shift(request, pk)
        services.review_shift(user=request.user, shift=shift, notes=request.data.get("notes", ""))
        audit.log("shift_reviewed", actor=request.user, obj=shift, request=request)
        return Response(shift_detail(shift, request.user))


class ReadingPhotoView(APIView):
    """Optional evidence photo for a meter reading (anyone on the shift), or a dip / delivery note (managers only)."""

    permission_classes = [IsTenantUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, kind, pk):
        model = {"meter": PumpMeterReading, "dip": TankReading, "delivery": Delivery}.get(kind)
        if model is None or "photo" not in request.FILES:
            raise ValidationError({"detail": "Send a photo."})
        if model is not PumpMeterReading:
            require(request.user, "tank_records")
        try:
            row = model.objects.filter(pk=pk).first()
        except DjangoValidationError:
            row = None
        if row is None or (model is PumpMeterReading and not can(request.user, "see_all_shifts") and not services.attendant_row(row.shift, request.user)):
            raise NotFound()
        row.photo = validate_image(request.FILES["photo"])
        row.save(update_fields=["photo", "updated_at"])
        return Response({"ok": True})


class TankDipView(APIView):
    """Dips: managers (their stations) and owners only. Attendants are refused, even to look (rule 11)."""

    permission_classes = [TankRecords]

    def get(self, request):
        qs = scope(TankReading.objects.select_related("recorded_by"), request.user, "tank__station_id")
        if request.query_params.get("tank"):
            qs = qs.filter(tank_id=request.query_params["tank"])
        return Response(TankReadingSerializer(qs[:200], many=True).data)

    def post(self, request):
        data = TankDipInput(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        _check_station(request.user, d["tank"].station)
        existed = bool(d.get("client_id")) and TankReading.objects.filter(client_id=d["client_id"]).exists()
        row = services.record_tank_dip(user=request.user, tank=d["tank"], litres=d.get("litres"), dip_mm=d.get("dip_mm"), at=d.get("at"), shift=d.get("shift"), client_id=d.get("client_id"))
        if not existed:
            audit.log("tank_dip_recorded", actor=request.user, obj=row, changes={"litres": str(row.litres), "tank": d["tank"].name}, request=request)
        return Response(TankReadingSerializer(row).data, status=status.HTTP_200_OK if existed else status.HTTP_201_CREATED)


class DeliveryView(APIView):
    """Deliveries: recorded by the manager or owner who received the fuel, never by an attendant (rule 11)."""

    permission_classes = [TankRecords]

    def get(self, request):
        qs = scope(Delivery.objects.select_related("recorded_by"), request.user, "tank__station_id")
        if request.query_params.get("tank"):
            qs = qs.filter(tank_id=request.query_params["tank"])
        return Response(DeliverySerializer(qs[:200], many=True).data)

    def post(self, request):
        data = DeliveryInput(data=request.data)
        data.is_valid(raise_exception=True)
        d = data.validated_data
        _check_station(request.user, d["tank"].station)
        existed = bool(d.get("client_id")) and Delivery.objects.filter(client_id=d["client_id"]).exists()
        row = services.record_delivery(user=request.user, tank=d["tank"], litres=d["litres"], delivered_at=d.get("delivered_at"), supplier=d["supplier"], note_number=d["note_number"], client_id=d.get("client_id"))
        if not existed:
            audit.log("delivery_recorded", actor=request.user, obj=row, changes={"litres": str(row.litres_received), "tank": d["tank"].name}, request=request)
        return Response(DeliverySerializer(row).data, status=status.HTTP_200_OK if existed else status.HTTP_201_CREATED)


# ---- private media (photos) ------------------------------------------------------------------------------------

MEDIA_FIELDS = {
    ("stations", "pump"): {"nameplate_photo", "display_photo"},
    ("operations", "pumpmeterreading"): {"photo"},
    ("operations", "tankreading"): {"photo"},
    ("operations", "delivery"): {"photo"},
    ("tenants", "tenantsettings"): {"logo"},
}
TANK_SIDE_MEDIA = {"tankreading", "delivery"}


class MediaView(APIView):
    """Photos are private to their tenant: the row is looked up through RLS before the file is served."""

    permission_classes = [IsTenantUser]

    def get(self, request, model, pk, field):
        if model in TANK_SIDE_MEDIA:
            require(request.user, "tank_records")
        for (app_label, name), fields in MEDIA_FIELDS.items():
            if name == model and field in fields:
                try:
                    obj = django_apps.get_model(app_label, name).objects.filter(pk=pk).first()
                except DjangoValidationError:
                    obj = None
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

def _valid(serializer_class, payload):
    data = serializer_class(data=payload)
    data.is_valid(raise_exception=True)
    return data.validated_data


def _apply(user, kind, p):
    """One queued entry from a phone. Never blocks on odd numbers (they are flagged); only truly impossible
    entries (unknown ids, reviewed shifts, someone else's shift, tank records from an attendant) are rejected
    and kept for a manager."""
    if kind == "shift_start":
        shift = do_shift_start(user, _valid(ShiftStartInput, p))
        result = {"shift_id": str(shift.pk)}
        if shift.merged_from is not None:
            result["merged"] = True
            if shift.merged_from:
                result["client_shift_id"] = shift.merged_from  # later entries from that phone use this id
        return result
    if kind in ("tank_dip", "delivery"):
        require(user, "tank_records", "Dips and deliveries are recorded by a manager.")
        if kind == "tank_dip":
            d = _valid(TankDipInput, p)
            _check_station(user, d["tank"].station)
            row = services.record_tank_dip(user=user, tank=d["tank"], litres=d.get("litres"), dip_mm=d.get("dip_mm"), at=d.get("at"), shift=d.get("shift"), client_id=d.get("client_id"))
        else:
            d = _valid(DeliveryInput, p)
            _check_station(user, d["tank"].station)
            row = services.record_delivery(user=user, tank=d["tank"], litres=d["litres"], delivered_at=d.get("delivered_at"), supplier=d["supplier"], note_number=d["note_number"], client_id=d.get("client_id"))
        return {"id": str(row.pk)}

    handlers = {"shift_end", "checkpoint", "shift_join", "shift_leave", "shift_cash", "shift_close", "pump_reassign"}
    if kind not in handlers:
        raise ValidationError({"detail": f"Unknown entry type '{kind}'."})
    shift = _resolve_shift(user, p.get("shift_id"))
    if kind == "shift_end":
        shift = do_shift_end(user, shift, _valid(ShiftEndInput, p))
        return {"shift_id": str(shift.pk), "status": shift.status, "flags": shift.flags}
    if kind == "checkpoint":
        d = _valid(CheckpointInput, p)
        _conflict_as_400(services.add_checkpoint, user=user, shift=shift, nozzle=d["nozzle"], reading=d["reading"], at=d.get("at"))
    elif kind == "shift_join":
        d = _valid(PersonInput, p)
        _conflict_as_400(services.join_shift, user=user, shift=shift, who=_user(d.get("user"), user.tenant_id), at=d.get("at"))
    elif kind == "shift_leave":
        d = _valid(PersonInput, p)
        _conflict_as_400(services.leave_shift, user=user, shift=shift, who=_user(d.get("user"), user.tenant_id), at=d.get("at"))
    elif kind == "shift_cash":
        d = _valid(CashInputOnly, p)
        services.require_on_shift(user, shift)
        _conflict_as_400(services.submit_cash, user=user, shift=shift, cash=d["cash"], who=_user(d.get("user"), user.tenant_id), at=d.get("at"))
    elif kind == "shift_close":
        d = _valid(PersonInput, p)
        _conflict_as_400(services.request_close, user=user, shift=shift, at=d.get("at"))
    elif kind == "pump_reassign":
        d = _valid(ReassignInput, p)
        _conflict_as_400(services.reassign_pump, user=user, shift=shift, pump=d["pump"], to_user=_user(d["user"], user.tenant_id), readings=d["readings"], at=d.get("at"))
    shift.refresh_from_db()
    return {"shift_id": str(shift.pk), "status": shift.status}


def _error_text(exc):
    detail = getattr(exc, "detail", None)
    return str(detail if detail is not None else exc)[:500]


class SyncView(APIView):
    """Offline sync: one batch call per shift (the phone groups its queue by shift), with a result per entry.

    Each entry carries a client-made op_id, so retrying is always safe. Rules: entries apply in the order sent; a repeated
    op_id returns the stored result and changes nothing; meter numbers that look odd are saved and flagged, never refused;
    a second person's different number is kept and flagged "readings differ"; entries for a reviewed shift are rejected and
    listed for a manager. Two phones on one shift each send their own batch; totals come from the merged data."""

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


class ShiftImportView(APIView):
    """Owner: import past shifts from CSV (multipart: `meters`, optional `cash`). `commit=true` saves them; without
    it the files are only checked and a summary of what would be saved comes back. All or nothing."""

    permission_classes = [IsOwner]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        if "meters" not in request.FILES:
            return Response({"errors": [{"file": "meters", "row": None, "message": "Choose the meters file."}]}, status=status.HTTP_400_BAD_REQUEST)
        commit = str(request.data.get("commit", "")).lower() in ("1", "true", "yes")
        try:
            summary = imports.import_shifts(user=request.user, meters=request.FILES["meters"], cash=request.FILES.get("cash"), commit=commit, request=request)
        except imports.ImportProblems as problems:
            return Response({"errors": problems.items}, status=status.HTTP_400_BAD_REQUEST)
        return Response(summary, status=status.HTTP_201_CREATED if commit else status.HTTP_200_OK)


class ShiftImportTemplateView(APIView):
    """Owner: the CSV templates (?file=meters or ?file=cash), with this business's stations, pumps and nozzles."""

    permission_classes = [IsOwner]

    def get(self, request):
        kind = "cash" if request.query_params.get("file") == "cash" else "meters"
        resp = HttpResponse(imports.template_csv(kind), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{kind}-template.csv"'
        return resp


# ---- end-of-shift approval and owner-allowed undo (apps/operations/approvals.py) ---------------------------------

class FixInput(serializers.Serializer):
    cash = CashInput(many=True, required=False, default=list)
    readings = ReadingInput(many=True, required=False, default=list)


class ShiftEndListView(APIView):
    """Managers (their stations) and the owner: ends of shift waiting for approval, and those sent back to fix.
    ?status=pending or ?status=sent_back narrows it."""

    permission_classes = [Allow("approve_shift_end")]

    def get(self, request):
        wanted = request.query_params.get("status")
        status_filter = wanted if wanted in (ShiftAttendant.Approval.PENDING, ShiftAttendant.Approval.SENT_BACK) else None
        return Response([approvals.end_card(r) for r in approvals.ends_for(request.user, status_filter)[:200]])


class ShiftEndDetailView(APIView):
    """One end of shift: the attendant's own, or (managers, owner) one at their stations."""

    permission_classes = [IsTenantUser]

    def get(self, request, pk):
        return Response(approvals.end_card(approvals.get_end(request.user, pk)))


class ShiftEndApproveView(APIView):
    permission_classes = [Allow("approve_shift_end")]

    def post(self, request, pk):
        row = approvals.approve(user=request.user, row=approvals.get_end(request.user, pk), request=request)
        return Response(approvals.end_card(row))


class ShiftEndSendBackView(APIView):
    permission_classes = [Allow("approve_shift_end")]

    def post(self, request, pk):
        row = approvals.send_back(user=request.user, row=approvals.get_end(request.user, pk), note=request.data.get("note", ""), request=request)
        return Response(approvals.end_card(row))


class ShiftEndFixView(APIView):
    """The attendant fixes their handed-in end ({"cash": [...], "readings": [...]}) when the manager sent it back or
    the owner allowed it. Needs signal: the answer depends on the server."""

    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        data = FixInput(data=request.data)
        data.is_valid(raise_exception=True)
        row = _conflict_as_400(approvals.fix_end, user=request.user, row=approvals.get_end(request.user, pk),
                               cash=data.validated_data["cash"], readings=data.validated_data["readings"], request=request)
        return Response(approvals.end_card(row))


class ShiftEndUndoView(APIView):
    """Ask the owner for permission to change something already handed in: {"kind": "cash" | "meters" | "reopen", "reason"}."""

    permission_classes = [IsTenantUser]

    def post(self, request, pk):
        req = approvals.request_undo(user=request.user, row=approvals.get_end(request.user, pk), kind=request.data.get("kind", ""),
                                     reason=request.data.get("reason", ""), request=request)
        return Response(approvals._undo_json(req), status=status.HTTP_201_CREATED)


class UndoRequestListView(APIView):
    """Owner: undo requests (?status=pending by default, or allowed / refused / used / all)."""

    permission_classes = [Allow("allow_undo")]

    def get(self, request):
        wanted = request.query_params.get("status", "pending")
        rows = approvals.undo_requests_for(request.user, None if wanted == "all" else wanted)
        return Response([approvals.undo_card(r) for r in rows.order_by("-created_at")[:200]])


class UndoDecideView(APIView):
    permission_classes = [Allow("allow_undo")]

    def post(self, request, pk, decision):
        if decision not in ("allow", "refuse"):
            raise NotFound()
        try:
            req = UndoRequest.objects.select_related("attendant__user", "attendant__shift__station", "shift__station").filter(pk=pk).first()
        except DjangoValidationError:
            req = None
        if req is None:
            raise NotFound("Request not found.")
        req = approvals.decide_undo(user=request.user, req=req, allow=decision == "allow", note=request.data.get("note", ""), request=request)
        return Response(approvals.undo_card(req))


class ApprovalSummaryView(APIView):
    """Counts for the menu badge: ends to approve (managers, owner) and undo requests to answer (owner)."""

    permission_classes = [Allow("approve_shift_end")]

    def get(self, request):
        ends = approvals.ends_for(request.user, ShiftAttendant.Approval.PENDING).count()
        undos = UndoRequest.objects.filter(status=UndoRequest.Status.PENDING).count() if can(request.user, "allow_undo") else 0
        return Response({"ends": ends, "undos": undos})
