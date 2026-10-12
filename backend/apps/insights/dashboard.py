from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.operations.models import Shift
from apps.reports.reconciliation import cash_summary, tank_range
from apps.reports.sales import daily_sales
from apps.stations.access import scope
from apps.stations.models import Station, Tank
from apps.tenants.services import get_settings

from .forecast import forecast_tank
from .models import Alert


def build_dashboard(user):
    now = timezone.now()
    today = timezone.localdate()
    cfg = get_settings(user.tenant_id)
    stations = list(scope(Station.objects.filter(status="active"), user, "id"))
    station_ids = [s.pk for s in stations]
    tanks = list(Tank.objects.filter(station_id__in=station_ids).select_related("fuel_type", "station"))
    forecasts = {t.pk: forecast_tank(t, cfg, now) for t in tanks}
    losses = [r for r in tank_range(tanks, today - timedelta(days=6), today) if r["status"] == "loss"]
    week_shifts = Shift.objects.filter(station_id__in=station_ids, status__in=["closed", "reviewed"], ended_at__gte=now - timedelta(days=7)).select_related("station")
    flagged_cash = [s for s in week_shifts if cash_summary(s)["flagged"]]

    out_stations, total_litres, total_amount = [], Decimal(0), {}
    for st in stations:
        sales = daily_sales(station_ids=[st.pk], date_from=today, date_to=today)["totals"]
        total_litres += Decimal(sales["litres"])
        for c, a in sales["amount_by_currency"].items():
            total_amount[c] = str(Decimal(total_amount.get(c, "0")) + Decimal(a))
        open_shifts = Shift.objects.filter(station=st, status="open").select_related("attendant")
        out_stations.append({
            "id": str(st.pk), "name": st.name, "today": sales,
            "tanks": [{k: f.get(k) for k in ("tank_id", "tank", "fuel_code", "current_litres", "capacity_litres", "level_pct", "days_to_empty", "refill_by", "low", "status", "as_of")} for t in tanks if t.station_id == st.pk for f in [forecasts[t.pk]]],
            "open_shifts": [{"id": str(s.pk), "attendant": s.attendant.full_name, "started_at": s.started_at, "hours_open": round((now - s.started_at).total_seconds() / 3600, 1)} for s in open_shifts],
            "unexplained_loss_7d": str(sum((Decimal(r["loss"]) for r in losses if r["tank_id"] in {str(t.pk) for t in tanks if t.station_id == st.pk}), Decimal(0))),
        })
    refills = sorted((f for f in forecasts.values() if f.get("refill_by")), key=lambda f: f["refill_by"])
    open_alerts = Alert.objects.filter(status="open")
    if user.role != "owner":
        open_alerts = open_alerts.filter(station_id__in=station_ids)
    return {
        "as_of": now,
        "totals": {"today_litres": str(total_litres), "today_amount_by_currency": total_amount, "open_alerts": open_alerts.count(),
                   "unexplained_loss_7d": str(sum((Decimal(r["loss"]) for r in losses), Decimal(0))), "flagged_cash_shifts_7d": len(flagged_cash),
                   "open_shifts": sum(len(s["open_shifts"]) for s in out_stations)},
        "next_refill": ({"tank": refills[0]["tank"], "refill_by": refills[0]["refill_by"], "days_to_empty": refills[0]["days_to_empty"]} if refills else None),
        "stations": out_stations,
        "alerts": [{"id": str(a.pk), "kind": a.kind, "severity": a.severity, "message": a.message, "created_at": a.created_at} for a in open_alerts.order_by("-created_at")[:5]],
    }
