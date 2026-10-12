import hashlib
import json

from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, HttpResponseNotModified
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOwner, IsOwnerOrManager
from apps.audit import services as audit
from apps.stations.access import scope
from apps.stations.models import Tank

from .alerts import evaluate_tenant
from .dashboard import build_dashboard
from .forecast import forecast_tank
from .models import Alert, OutboundMessage
from .serializers import AlertSerializer, OutboundMessageSerializer
from .summary import build_daily_summary
from .tasks import send_summary_for


class DashboardView(APIView):
    """One small call for the whole phone dashboard. Cached briefly and supports ETag, so slow connections stay quick."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        body = json.dumps(build_dashboard(request.user), cls=DjangoJSONEncoder, separators=(",", ":"))
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
    permission_classes = [IsOwnerOrManager]
    serializer_class = AlertSerializer

    def get_queryset(self):
        qs = scope(Alert.objects.select_related("station"), self.request.user, "station_id")
        return qs.filter(status=self.request.query_params.get("status", "open"))


class AlertResolveView(APIView):
    permission_classes = [IsOwnerOrManager]

    def post(self, request, pk):
        alert = scope(Alert.objects.all(), request.user, "station_id").filter(pk=pk).first()
        if alert is None:
            raise NotFound()
        if alert.status == "open":
            alert.status, alert.resolved_at, alert.resolved_by = "resolved", timezone.now(), request.user
            alert.save()
            audit.log("alert_resolved", actor=request.user, obj=alert, changes={"message": alert.message}, request=request)
        return Response(AlertSerializer(alert).data)


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
