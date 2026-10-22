from datetime import date, timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOwner, IsOwnerOrManager, TankRecords
from apps.operations import tradingday
from apps.operations.models import Shift
from apps.stations.access import visible_station_ids

from . import reconciliation, sales
from .tank_history import tank_history


def _dates(request, default_days=30):
    q = request.query_params
    try:
        to = date.fromisoformat(q["date_to"]) if q.get("date_to") else tradingday.today()
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
        totals = reconciliation.tank_totals(rows)  # the running difference per tank over the period, before any filtering
        if request.query_params.get("flagged") == "1":
            rows = [r for r in rows if r["flagged"]]
        return Response({"rows": rows, "totals": totals})


def _closed_shifts(request, with_open=False):
    """Shifts started in the chosen trading days (21:00 to 21:00) and stations. with_open: also shifts still running,
    for their finished parts."""
    frm, to = _dates(request, default_days=14)
    start, end = tradingday.bounds(None, frm)[0], tradingday.bounds(None, to)[1]
    qs = Shift.objects.select_related("station").prefetch_related("attendants__user").filter(started_at__gt=start, started_at__lte=end)
    if not with_open:
        qs = qs.exclude(status="open")
    ids = _stations(request)
    if ids is not None:
        qs = qs.filter(station_id__in=ids)
    return qs


class AttendantReportView(APIView):
    """Managers and owners: litres sold per pump and cash handed in, per attendant, over a period."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        return Response({"rows": reconciliation.attendant_report(_closed_shifts(request, with_open=True).order_by("started_at")[:2000])})


class CashReconciliationView(APIView):
    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        qs = _closed_shifts(request)
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


class TankHistoryView(APIView):
    """Dip history: when each tank was dipped and by whom, deliveries, and days without a dip (managers, owners)."""

    permission_classes = [TankRecords]

    def get(self, request):
        frm, to = _dates(request, default_days=14)
        return Response(tank_history(station_ids=_stations(request), date_from=frm, date_to=to))


class AccountingPackView(APIView):
    """Owner only: the monthly accounting pack, one Excel workbook per station and month. ?station=<id>&month=2026-10"""

    permission_classes = [IsOwner]

    def get(self, request):
        from django.http import HttpResponse

        from apps.stations.models import Station

        from .accounting import build_pack

        try:
            year, month = (int(x) for x in request.query_params.get("month", "").split("-"))
            date(year, month, 1)
        except ValueError:
            raise ValidationError({"month": "Choose a month, like 2026-10."})
        if not request.query_params.get("station"):
            raise ValidationError({"station": "Choose a station."})
        wanted = _stations(request)[0]  # refuses a station this manager does not run
        try:
            station = Station.objects.filter(pk=wanted).first()
        except DjangoValidationError:
            station = None
        if station is None:
            raise ValidationError({"station": "Not available."})
        body = build_pack(station, year, month)
        name = f"Accounting pack - {station.name} - {year}-{month:02d}.xlsx"
        resp = HttpResponse(body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="{name}"'
        return resp


class DaysReportView(APIView):
    """Reports by day: the daily report of every trading day in the dates (newest first, at most 31), per station;
    each day's shifts are its drill-down."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        from apps.stations.models import Station

        from .daily import daily_report

        frm, to = _dates(request, default_days=14)
        to = min(to, tradingday.today())  # no empty days ahead
        frm = max(frm, to - timedelta(days=30))
        ids = _stations(request)
        stations = Station.objects.filter(status="active")
        if ids is not None:
            stations = stations.filter(pk__in=ids)
        out = []
        day = to
        while day >= frm:
            out.extend(daily_report(st, day) for st in stations.order_by("name"))
            day -= timedelta(days=1)
        return Response({"days": out})


class DailyReportView(APIView):
    """Owner and managers (their stations): the daily report for a station and trading day (21:00 to 21:00, named by
    the closing day). ?station=<id>&day=2026-10-22; without a day, the last closed day (else the day so far)."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        from apps.stations.models import Station

        from .daily import daily_report, last_closed_day

        ids = visible_station_ids(request.user)
        stations = Station.objects.filter(status="active")
        if ids is not None:
            stations = stations.filter(pk__in=ids)
        if request.query_params.get("station"):
            stations = stations.filter(pk=_stations(request)[0])
        out = []
        for st in stations.order_by("name"):
            try:
                day = date.fromisoformat(request.query_params["day"]) if request.query_params.get("day") else (last_closed_day(st) or tradingday.today(st.pk))
            except ValueError:
                raise ValidationError({"day": "Dates must look like 2026-10-22."})
            out.append(daily_report(st, day))
        return Response(out)
