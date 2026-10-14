from datetime import date, timedelta

from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOwnerOrManager
from apps.operations.models import Shift
from apps.stations.access import visible_station_ids

from . import reconciliation, sales


def _dates(request, default_days=30):
    q = request.query_params
    try:
        to = date.fromisoformat(q["date_to"]) if q.get("date_to") else timezone.localdate()
        frm = date.fromisoformat(q["date_from"]) if q.get("date_from") else to - timedelta(days=default_days - 1)
    except ValueError:
        raise ValidationError({"detail": "Dates must look like 2026-10-31."})
    if frm > to or (to - frm).days > 366:
        raise ValidationError({"detail": "Choose a period of up to one year."})
    return frm, to


def _stations(request):
    ids = visible_station_ids(request.user)
    wanted = request.query_params.get("station")
    if wanted:
        if ids is not None and wanted not in {str(i) for i in ids}:
            raise ValidationError({"station": "Not available."})
        return [wanted]
    return ids


class DailySalesView(APIView):
    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        frm, to = _dates(request)
        return Response(sales.daily_sales(station_ids=_stations(request), date_from=frm, date_to=to))


class TankReconciliationView(APIView):
    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        frm, to = _dates(request, default_days=14)
        ids = visible_station_ids(request.user)
        rows = reconciliation.tank_range(reconciliation.tanks_for(ids, request.query_params.get("station")), frm, to)
        if request.query_params.get("flagged") == "1":
            rows = [r for r in rows if r["flagged"]]
        return Response({"rows": rows})


class CashReconciliationView(APIView):
    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        frm, to = _dates(request, default_days=14)
        qs = Shift.objects.select_related("station").prefetch_related("attendants__user").filter(started_at__date__gte=frm, started_at__date__lte=to).exclude(status="open")
        ids = _stations(request)
        if ids is not None:
            qs = qs.filter(station_id__in=ids)
        rows = []
        for s in qs.order_by("-started_at")[:300]:
            people = list(s.attendants.all())
            rows.append({
                "shift_id": str(s.pk), "station": s.station.name, "started_at": s.started_at, "ended_at": s.ended_at,
                "attendant": next((a.user.full_name for a in people if a.is_lead), None), "attendants": [a.user.full_name for a in people],
                **reconciliation.cash_summary(s),
            })
        if request.query_params.get("flagged") == "1":
            rows = [r for r in rows if r["flagged"]]
        return Response({"rows": rows})
