import hashlib
import json

from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, HttpResponseNotModified
from rest_framework.exceptions import NotFound
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOwner, IsOwnerOrManager, can
from apps.audit import services as audit
from apps.operations.services import pump_gaps
from apps.stations.access import scope
from apps.stations.models import Station, Tank

from . import alert_actions
from .alerts import evaluate_tenant
from .dashboard import build_dashboard
from .forecast import forecast_tank
from .models import Alert, OutboundMessage
from .serializers import AlertEventSerializer, AlertSerializer, OutboundMessageSerializer
from .summary import build_daily_summary
from .tasks import send_summary_for


class DashboardView(APIView):
    """One small call for the whole phone dashboard. Cached briefly and supports ETag, so slow connections stay quick."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        # Pumps nobody is working are checked on every call, outside the cache: a manager who just gave a pump to
        # someone sees the warning go at once.
        stations = scope(Station.objects.filter(status="active"), request.user, "id")
        data = {**build_dashboard(request.user), "pump_gaps": pump_gaps(stations)}
        body = json.dumps(data, cls=DjangoJSONEncoder, separators=(",", ":"))
        # as_of changes every second; the validator ignores it so an unchanged dashboard gets a 304
        stable = json.dumps({k: v for k, v in json.loads(body).items() if k != "as_of"}, sort_keys=True)
        etag = '"' + hashlib.md5(stable.encode()).hexdigest() + '"'  # noqa: S324 - cache validator only
        if request.headers.get("If-None-Match") == etag:
            resp = HttpResponseNotModified()
        else:
            resp = HttpResponse(body, content_type="application/json")
        resp["ETag"] = etag
        resp["Cache-Control"] = "private, max-age=30"
        return resp


class ForecastView(APIView):
    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        tanks = scope(Tank.objects.select_related("fuel_type", "station"), request.user, "station_id")
        if request.query_params.get("station"):
            tanks = tanks.filter(station_id=request.query_params["station"])
        return Response({"tanks": [forecast_tank(t) for t in tanks]})


class AlertListView(ListAPIView):
    """?view=open (default): open and waiting for approval, oldest first · ?view=review (owner): write-offs to approve and
    managers' resolutions to accept · ?view=history: everything, newest first, filtered by station, kind, status, dates."""

    permission_classes = [IsOwnerOrManager]
    serializer_class = AlertSerializer

    def get_queryset(self):
        q = self.request.query_params
        base = Alert.objects.select_related("station", "resolved_by", "reviewed_by")
        view = q.get("view", "history" if q.get("status") else "open")
        if view == "review":
            qs = alert_actions.to_review().select_related("station", "resolved_by", "reviewed_by") if can(self.request.user, "owner") else base.none()
        else:
            qs = base
        qs = scope(qs, self.request.user, "station_id")
        if view == "open":
            return qs.filter(status__in=[alert_actions.OPEN, alert_actions.AWAITING]).order_by("created_at")
        if view == "history":
            for key, lookup in (("station", "station_id"), ("kind", "kind"), ("status", "status"), ("date_from", "created_at__date__gte"), ("date_to", "created_at__date__lte")):
                if q.get(key):
                    qs = qs.filter(**{lookup: q[key]})
            return qs.order_by("-created_at")
        return qs.order_by("resolved_at", "created_at")


def _alert(request, pk):
    alert = scope(Alert.objects.select_related("station", "resolved_by", "reviewed_by"), request.user, "station_id").filter(pk=pk).first()
    if alert is None:
        raise NotFound()
    return alert


def _with_story(alert):
    return {**AlertSerializer(alert).data, "events": AlertEventSerializer(alert.events.select_related("actor"), many=True).data}


class AlertDetailView(APIView):
    """One alert with its story: every step, who did it and when."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request, pk):
        return Response(_with_story(_alert(request, pk)))


class AlertResolveView(APIView):
    """Manager (their stations) or owner: {"reason": <code>, "note": "..."}. A manager's write-off waits for the owner."""

    permission_classes = [IsOwnerOrManager]

    def post(self, request, pk):
        alert = alert_actions.resolve(user=request.user, alert=_alert(request, pk), reason=request.data.get("reason", ""), note=request.data.get("note", ""))
        audit.log("alert_resolved", actor=request.user, obj=alert, request=request,
                  changes={"message": alert.message, "reason": alert.resolution_reason, "note": alert.resolution_note, "status": alert.status})
        return Response(_with_story(alert))


class AlertApproveView(APIView):
    permission_classes = [IsOwner]

    def post(self, request, pk):
        alert = alert_actions.approve(user=request.user, alert=_alert(request, pk), note=request.data.get("note", ""))
        audit.log("alert_write_off_approved", actor=request.user, obj=alert, request=request, changes={"reason": alert.resolution_reason})
        return Response(_with_story(alert))


class AlertAcceptView(APIView):
    permission_classes = [IsOwner]

    def post(self, request, pk):
        return Response(_with_story(alert_actions.accept(user=request.user, alert=_alert(request, pk))))


class AlertSendBackView(APIView):
    permission_classes = [IsOwner]

    def post(self, request, pk):
        alert = alert_actions.send_back(user=request.user, alert=_alert(request, pk), note=request.data.get("note", ""))
        audit.log("alert_sent_back", actor=request.user, obj=alert, request=request, changes={"note": request.data.get("note", "")})
        return Response(_with_story(alert))


class AlertSummaryView(APIView):
    """Counts for the menu badge, and the reasons people can pick per kind of alert."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        mine = scope(Alert.objects.all(), request.user, "station_id")
        return Response({
            "open": mine.filter(status=alert_actions.OPEN).count(),
            "awaiting_approval": mine.filter(status=alert_actions.AWAITING).count(),
            "to_review": scope(alert_actions.to_review(), request.user, "station_id").count() if can(request.user, "owner") else 0,
            "reasons": {kind: [{"code": c, "label": label, "write_off": w} for c, label, w in rows] for kind, rows in alert_actions.REASONS.items()},
        })


class AlertRefreshView(APIView):
    """Re-check now instead of waiting for the hourly job."""

    permission_classes = [IsOwnerOrManager]

    def post(self, request):
        return Response(evaluate_tenant(request.user.tenant_id))


class SummaryPreviewView(APIView):
    permission_classes = [IsOwner]

    def get(self, request):
        return Response({"text": build_daily_summary(request.user.tenant_id)})

    def post(self, request):
        """Send today's summary now (to test the channel)."""
        msg = send_summary_for(request.user.tenant_id)
        return Response(OutboundMessageSerializer(msg).data)


class MessageLogView(ListAPIView):
    permission_classes = [IsOwner]
    serializer_class = OutboundMessageSerializer
    queryset = OutboundMessage.objects.all()
